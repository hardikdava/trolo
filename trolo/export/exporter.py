from typing import Dict, Union, Optional, List, Tuple

import os
import sys
from pathlib import Path
import traceback

import numpy as np
import torch
import torch.nn as nn

from ..loaders import YAMLConfig
from ..loaders.maps import get_model_config_path
from ..utils.smart_defaults import infer_pretrained_model, infer_model_config_path, infer_device
from ..utils.logging.glob_logger import LOGGER


class ModelExporter:

    def __init__(
        self, 
        model : Union[str, Path] = None,
        config : Union[str, Path] =  None, 
        device : Optional[str] = None,
    ):
        """Initialize detection predictor

        Args:
            model: Model name (e.g. 'dfine-n') or path to checkpoint
            config: Optional config name or path. If None, will try to:
                   1. Load from checkpoint if available
                   2. export from model name
            device: device on export will be run
        """
        if model is None:
            raise ValueError("Must specify model name or checkpoint path")
        model_path = model
        # Convert model to path if it's a name
        self.model_path = infer_pretrained_model(model_path)
        if os.path.exists(model):
            LOGGER.error(f"{model} not found")

        # Load checkpoint first to check for config
        checkpoint = torch.load(self.model_path, map_location="cpu", weights_only=False)

        if config is None:
            if "cfg" in checkpoint:
                LOGGER.info("Loading config from checkpoint")
                self.config = YAMLConfig.from_state_dict(checkpoint["cfg"])
            else:
                LOGGER.warning("Config not found in checkpoint, inferring from model name")
                config = infer_model_config_path(model)
                self.config = self.load_config(config)
        else:
            # Convert config to path if it's a name
            if isinstance(config, str) and not Path(config).exists():
                config = get_model_config_path(config)
            self.config = self.load_config(config)

        self.device = torch.device(infer_device(device))
        LOGGER.info(f"{self.device}")
        self.model = self.load_model(self.model_path)
        self.model.to(self.device)
        self.model.eval()

    def load_config(self, config_path: str) -> Dict:
        """Load config from YAML"""
        LOGGER.info(f"Loading config from {config_path}")
        cfg = YAMLConfig(config_path)
        return cfg
    
    def load_model(self, model: str) -> torch.nn.Module:
        """Load detection model using config"""
        # Load checkpoint
        checkpoint = torch.load(model, map_location="cpu", weights_only=False)

        if "HGNetv2" in self.config.yaml_cfg:
            self.config.yaml_cfg["HGNetv2"]["pretrained"] = False

        # Load model state
        if "ema" in checkpoint:
            state = checkpoint["ema"]["module"]
        else:
            state = checkpoint["model"]

        # Load state into config.model
        self.config.model.load_state_dict(state)

        # Create deployment model wrapper so post process is included
        class ModelWrapper(nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model

            def forward(self, images):
                # Run base model
                outputs = self.model(images)
                logits, boxes = outputs["pred_logits"], outputs["pred_boxes"]
                # Post-process outputs
                probs = logits.softmax(-1)
                scores, labels = probs.max(-1)
                return labels, boxes, scores

        # Create deployment model wrapper
        model = self.config.model.deploy()
        wrapped_model = ModelWrapper(model)
        return wrapped_model

    def export(
        self, 
        input_size : Union[List, Tuple[int, int], int] =  (640, 640),
        export_format : str = "onnx",
        fp16: Optional[bool] = False,
    ):
        if isinstance(input_size, int):
            input_size = (input_size, input_size)
        # check the model format
        if export_format is None:
            raise ValueError(f"Export format is missing!")

        LOGGER.info(f"Exporting {self.model_path} to {export_format}")
        if export_format.lower().strip() == "onnx":
            exported_path = self.export2onnx(
                input_size=torch.tensor(input_size)
            )
        elif export_format.lower().strip() == "openvino":
            exported_path = self.export_openvino(
                input_size=input_size,
                fp16=fp16,
            )

        elif export_format.lower().strip() == "engine" or  export_format.lower().strip() =="tensorrt" :
            exported_path = self.export_engine(
                input_size=input_size,
                dtype="fp32"
            )

        if not os.path.exists(exported_path):
            LOGGER.error(f"Failed to export model: {exported_path}")

        LOGGER.info(f"Model exported to {exported_path}")

    def export2onnx(
        self,
        input_size : Union[List, Tuple, torch.Tensor] = None,
        dynamic : Optional [bool] = False,
        batch_size : Optional[int] =  1,
        opset_version : Optional[int] = 16,
        simplify : Optional[bool] = False
    ) -> str:
        import onnx
        input_size  = torch.tensor(input_size)
        input_data = torch.rand(batch_size, 3, *input_size)
        input_data = input_data.to(self.device)

        filename, file_ext = os.path.splitext(self.model_path)
        exported_path  =  f"{filename}.onnx"
        if dynamic:
            dynamic_axes = {'images': {0: 'N', },'orig_target_sizes': {0: 'N'}}

        input_names = ['images']
        output_names = ['labels', 'boxes', 'scores']

        # dynamic only compatible with cpu do not use it with gpu
        torch.onnx.export(
            self.model.cpu() if dynamic else self.model,
            input_data.cpu() if dynamic else input_data,
            exported_path,
            input_names = input_names, 
            output_names = output_names,
            dynamic_axes=dynamic_axes if dynamic else None,
            opset_version=opset_version,
            verbose=False,
            do_constant_folding=True,
        )

        # Check the model
        onnx_model  = onnx.load(exported_path)
        onnx.checker.check_model(onnx_model)
        LOGGER.info(f"Model exported to ONNX: {exported_path}")

        if simplify:
            LOGGER.info("Simplifying the onnx model")
            import onnxsim
            onnx_model_simplified, check = onnxsim.simplify(exported_path)
            onnx.save(onnx_model_simplified, exported_path)        
            onnx_model  = onnx.load(exported_path)
            onnx.checker.check_model(onnx_model)
            LOGGER.info(f"Simplified model exported to ONNX: {exported_path}")

        return exported_path


    def export_openvino(
        self,
        input_size : Union[List, Tuple] = None,
        verbose : Optional [bool] = False,
        batch_size : Optional[int] =  1,
        fp16 : Optional[bool] = False
    ) -> str:

        import openvino as ov

        filename, file_ext = os.path.splitext(self.model_path)
        output_path = f"{filename}.xml"
        input_data = np.random.randn(batch_size, 3, *input_size).astype(np.float32) / 255.0
        ov_model = ov.convert_model(
            self.model.cpu(),
            input=[batch_size, 3, *input_size],
            example_input=input_data,
        )

        ov.runtime.save_model(ov_model, output_path, compress_to_fp16=fp16)
        return output_path

    def export_engine(
        self,
        input_size: Union[List, Tuple] = None,
        dtype: Optional[str] = "fp32",
        batch_size: Optional[int] = 1,
        verbose: Optional[bool] = False,
    ):
        # Check device
        if self.device is None or self.device == "cpu":
            raise ValueError(
                "TensorRT requires GPU export, but no device was specified. Please explicitly specify a GPU device (e.g., device=cuda:0) to proceed."
            )

        import tensorrt as trt

        if not self.model_path.endswith("onnx"):
            exported_path = self.export2onnx(input_size, batch_size=batch_size)
        else:
            exported_path = self.model_path

        if verbose:
            trt_logger = trt.Logger(trt.Logger.Severity.VERBOSE)
        else:
            trt_logger = trt.Logger(trt.Logger.Severity.WARNING)

        builder = trt.Builder(trt_logger)
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        parser = trt.OnnxParser(network, trt_logger)

        if not parser.parse_from_file(exported_path):
            raise RuntimeError(f"Failed to load ONNX file {exported_path}")

        config = builder.create_builder_config()
        if dtype.lower() == "fp16":
            config.flags |= 1 << int(trt.BuilderFlag.FP16)
        elif dtype.lower() == "int8":
            config.flags |= 1 << int(trt.BuilderFlag.INT8)
            raise NotImplementedError("INT8 calibration is not yet implemented.")

        try:
            engine = builder.build_serialized_network(network, config)
            if not engine:
                raise RuntimeError("Failed to build TensorRT engine.")
        except Exception as e:
            raise RuntimeError(f"Engine serialization failed: {str(e)}")

        filename = Path(self.model_path).stem
        engine_f = f"{filename}_{str(dtype)}.engine"
        with open(engine_f, "wb") as f:
            f.write(engine)

        LOGGER.info(f"TRT Engine saved to file: {engine_f}")
        return engine_f