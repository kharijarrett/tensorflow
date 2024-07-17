import os
from typing import ClassVar, Mapping, Sequence, Dict, Optional
from numpy.typing import NDArray
from typing_extensions import Self
from viam.services.mlmodel import MLModel, Metadata, TensorInfo
from viam.module.types import Reconfigurable
from viam.resource.types import Model, ModelFamily
from viam.proto.app.robot import ServiceConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.utils import ValueTypes
from viam.logging import getLogger

import numpy as np
import google.protobuf.struct_pb2 as pb
import tensorflow as tf




LOGGER = getLogger(__name__)


class TensorflowModule(MLModel, Reconfigurable):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "mlmodel"), "tensorflow-cpu")

    def __init__(self, name: str):
        super().__init__(name=name)

    @classmethod
    def new_service(
        cls, config: ServiceConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        service = cls(config.name)
        service.reconfigure(config, dependencies)
        return service

    @classmethod
    def validate_config(cls, config: ServiceConfig) -> Sequence[str]:
        model_path = config.attributes.fields["model_path"].string_value
        if model_path == "":
            raise Exception(
                "model_path must be the location of the Tensorflow SavedModel directory"
            )

        # Add trailing / if not there
        if model_path[-1] != "/":
            model_path = model_path + "/"

        # Check that model_path points to a dir with a pb file in it
        # and that the model file isn't too big (>500 MB)
        isValid = False
        for file in os.listdir(model_path):
            if ".pb" in file:
                isValid = True
                sizeMB = os.stat(model_path + file).st_size / (1024 * 1024)
                if sizeMB > 500:
                    LOGGER.warn(
                        "model file may be large for certain hardware ("
                        + str(sizeMB)
                        + "MB)"
                    )
        if not isValid:
            raise Exception(
                "model_path must be the location of a SavedModel directory with a .pb file"
            )

        return []

    def reconfigure(
        self, config: ServiceConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        self.model_path = config.attributes.fields["model_path"].string_value
        self.label_path = config.attributes.fields["label_path"].string_value

        # This is where we do the actual loading of the model
        self.model = tf.saved_model.load(self.model_path)

        # Save the input_info and output_info as a list of tuples,
        # each being a tensor with (name, shape, underlying type)
        self.input_info = []
        self.output_info = []

        f = self.model.signatures["serving_default"]

        # f.inputs may include "empty" inputs as resources, but _arg_keywords only contains input tensor names
        if len(f._arg_keywords) <= len(f.inputs):  # should always be true tbh
            for i in range(len(f._arg_keywords)):
                ff = f.inputs[i]
                if ff.dtype != "resource":  # probably unneccessary to check now
                    info = (f._arg_keywords[i], prepShape(ff.get_shape()), ff.dtype)
                    self.input_info.append(info)

        for out in f.outputs:
            info = (out.name, prepShape(out.get_shape()), out.dtype)
            self.output_info.append(info)

    async def infer(
        self, input_tensors: Dict[str, NDArray], *, timeout: Optional[float] = None
    ) -> Dict[str, NDArray]:
        """Take an already ordered input tensor as an array, make an inference on the model, and return an output tensor map.

        Args:
            input_tensors (Dict[str, NDArray]): A dictionary of input flat tensors as specified in the metadata

        Returns:
            Dict[str, NDArray]: A dictionary of output flat tensors as specified in the metadata
        """

        # Check input against expected length
        inputVars = list(input_tensors.keys())
        if len(inputVars) > len(self.input_info):
            raise Exception(
                "there are more input tensors ("
                + str(len(inputVars))
                + ") than the model expected ("
                + str(len(self.input_info))
                + ")"
            )

        # Prepare input(s) for inference
        input_list = []
        for i in range(len(inputVars)):
            inputT = input_tensors[inputVars[i]]  # grab tensor
            inputT = tf.convert_to_tensor(
                inputT, dtype=self.input_info[i][2]
            )  # make into a tf tensor of right type
            input_list.append(inputT)  # put in list

        if len(inputVars) == 1:
            data = np.squeeze(np.asarray(input_list), axis=0)
        else:
            data = np.asarray(input_list)

        # Do the infer. res might have >1 tensor in it
        res = self.model(data)

        # Check output against expected length
        if len(self.output_info) < len(res):
            raise Exception(
                "there are more output tensors ("
                + str(len(res))
                + ") than the model expected ("
                + str(len(self.output_info))
                + ")"
            )

        # Prep outputs for return
        out = {}
        if len(res) > 1:
            for named_tensor in res:
                out[named_tensor] = np.asarray(res[named_tensor])
        else:
            name = self.output_info[0][0]
            out[name] = np.asarray(res[0])

        return out

    async def metadata(self, *, timeout: Optional[float] = None) -> Metadata:
        """Get the metadata (such as name, type, expected tensor/array shape, inputs, and outputs) associated with the ML model.

        Returns:
            Metadata: The metadata
        """

        extra = pb.Struct()
        extra["labels"] = self.label_path

        # Fill out input and output info
        input_info = []
        output_info = []
        for inputT in self.input_info:
            info = TensorInfo(
                name=inputT[0], shape=inputT[1], data_type=prepType(inputT[2])
            )
            input_info.append(info)

        for output in self.output_info:
            info = TensorInfo(
                name=output[0],
                shape=output[1],
                data_type=prepType(output[2]),
                extra=extra,
            )
            output_info.append(info)

        return Metadata(
            name="tensorflow_model", input_info=input_info, output_info=output_info
        )

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs
    ):
        return NotImplementedError


# Want to return a list of ints (-1 for None)
def prepShape(tensorShape):
    out = []
    for t in list(tensorShape):
        if t is None:
            out.append(-1)
        else:
            out.append(t)
    return out


# Want to return a simple string ("float32", "int64", etc.)
def prepType(tensorType):
    # The dtype uses an escaped apostrophe around the actual type name so use that
    s = str(tensorType)
    inds = [i for i, letter in enumerate(s) if letter == "'"]
    return s[inds[0] + 1 : inds[1]]
