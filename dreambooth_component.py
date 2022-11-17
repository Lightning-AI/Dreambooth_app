import lightning as L
import torch.cuda

from base_diffusion import BaseDiffusion

from diffusers import StableDiffusionPipeline
from diffusion_serve import DreamBoothInput
from utils import image_decode

PRETRAINED_MODEL_NAME = "CompVis/stable-diffusion-v1-4"
HF_TOKEN = "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"


class DreamBooth(BaseDiffusion):

    def setup(self, *args, **kwargs):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = StableDiffusionPipeline.from_pretrained(
            "CompVis/stable-diffusion-v1-4",
            **models.extras
        )

    def predict(self, data: DreamBoothInput):
        print("Predicting...")
        print(data.prompt)
        out = self._model(prompt=data.prompt, num_inference_steps=1)
        return {"image": image_decode(out[0][0])}


app = L.LightningApp(DreamBooth())

#
# import lightning as L
# import base64, io, models, base_diffusion
# from diffusers import StableDiffusionPipeline
#
#
# class DreamBoothDiffusion(base_diffusion.BaseDiffusion):
#
#     def __init__(self):
#         super().__init__()
#         self.weights_drive = L.storage.Drive("lit://weights")
#
#     def serialize(self, image):
#         buffered = io.BytesIO()
#         image.save(buffered, format="PNG")
#         return base64.b64encode(buffered.getvalue()).decode("utf-8")
#
#     def setup(self):
#         self.model = StableDiffusionPipeline.from_pretrained(
#             "CompVis/stable-diffusion-v1-4",
#             **models.extras
#         )
#
#     def predict(self, data):
#         images = self.model(prompt=data.prompt, num_inference_steps=1)[0]
#         return {"images": self.serialize(images)}
#
#
# app = L.LightningApp(DreamBoothDiffusion())
