import lightning as L
from lightning_diffusion import BaseDiffusion, DreamBoothTuner, models
from diffusers import StableDiffusionPipeline


class ServeDreamBoothDiffusion(BaseDiffusion):

    def setup(self):
        self._model = StableDiffusionPipeline.from_pretrained(
            **models.get_kwargs("CompVis/stable-diffusion-v1-4", self.weights_drive),
        ).to(self.device)

    def finetune(self):
        DreamBoothTuner(
            image_urls=[
                "https://huggingface.co/datasets/valhalla/images/resolve/main/2.jpeg",
                "https://huggingface.co/datasets/valhalla/images/resolve/main/3.jpeg",
                "https://huggingface.co/datasets/valhalla/images/resolve/main/5.jpeg",
                "https://huggingface.co/datasets/valhalla/images/resolve/main/6.jpeg",
                ## You can change or add additional images here
            ],
            prompt="a photo of [sks] [cat clay toy] [riding a bicycle]",
        ).run(self.model)

    def predict(self, data):
        out = self.model(prompt=data.prompt)
        return {"image": self.serialize(out[0][0])}



app = L.LightningApp(
    ServeDreamBoothDiffusion(
        serve_cloud_compute=L.CloudCompute("gpu", disk_size=80),
        finetune_cloud_compute=L.CloudCompute("gpu-fast", disk_size=80),
    )
)