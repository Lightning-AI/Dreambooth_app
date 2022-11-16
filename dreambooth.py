import lightning as L
from lightning.lite import LightningLite
from typing import List, Optional
from datasets import DreamBoothDataset, PromptDataset
import os
import torch
from diffusers import AutoencoderKL, DDPMScheduler, PNDMScheduler, StableDiffusionPipeline, UNet2DConditionModel
from transformers import CLIPFeatureExtractor, CLIPTextModel, CLIPTokenizer
import requests
import gc
import math
from lightning.app.components import LiteMultiNode




class _DreamBoothFineTunerWork(L.LightningWork):

    def __init__(
        self,
        image_urls: List[str],
        prompt: str,
        preservation_prompt: Optional[str] = None,
        pretrained_model_name_or_path: str = "CompVis/stable-diffusion-v1-4",
        revision: Optional[str] = "fp16",
        tokenizer_name: Optional[str] = None,
        max_steps: int = 1000,
        prior_loss_weight: float = 1.0,
        train_batch_size: int = 1,
        gradient_accumulation_steps: int = 1,
        learning_rate: float = 5e-6,
        lr_scheduler = "constant",
        lr_warmup_steps: int = 0,
        max_train_steps: int = 400,
        precision: int = 16,
        use_8bit_adam: bool = True,
        use_auth_token: str = "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",
        seed: int = 42,
        gradient_checkpointing: bool = True,
        cloud_compute: L.CloudCompute = L.CloudCompute("gpu"),
        resolution: int = 512,
        center_crop: bool = True,
        **kwargs,
    ):
        """
        The `DreamBoothFineTuner` fine-tunes stable diffusion models using the methodology introduced in

        DreamBooth: Fine Tuning Text-to-Image Diffusion Models for Subject-Driven Generation
        https://arxiv.org/abs/2208.12242

        Arguments:
            image_urls: List of image urls to fine-tune the models.
            prompt: The prompt to describe the images.
                Example: `A [V] dog` where `[V]` is a special name given for
                the diffusion model to learn about this new concept.
            preservation_prompt: The prompt used for the diffusion model to preserve knowledge.
                Example: `A dog`
            pretrained_model_name_or_path: The name of the model.
            revision: The revision commit for the model weights.
            tokenizer_name: The name of the tokenizer.
            prior_loss_weight: The weight of prior preservation loss to preserve knowledge.
            train_batch_size: The batch size used during training.
            gradient_accumulation_steps: Number of training batch before updating the weights.
            learning_rate: The learning rate to optimize the model.
            lr_scheduler: The LR scheduler to be used.
            lr_warmup_steps: The number of warmup steps.
            max_train_steps: The number of training steps to fine-tune the model.
            precision: The precision to be used for fine-tuning the model.
            use_8bit_adam: Whether to use 8 bit adam.
            seed: The seed to initialize the random initializers.
            resolution: The resolution of the image to train upon.
            center_crop: Whether to crop the images in the center
            kwargs: The keywords arguments passed down to the work.
        """
        super().__init__(cloud_compute=cloud_compute, **kwargs)

        # User Arguments
        self.image_urls = image_urls
        self.prompt = prompt
        self.preservation_prompt = preservation_prompt
        self.pretrained_model_name_or_path = pretrained_model_name_or_path
        self.revision = revision
        self.tokenizer_name = tokenizer_name
        self.max_steps = max_steps
        self.prior_loss_weight = prior_loss_weight
        self.train_batch_size = train_batch_size
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.learning_rate = learning_rate
        self.lr_scheduler = lr_scheduler
        self.lr_warmup_steps = lr_warmup_steps
        self.max_train_steps = max_train_steps
        self.precision = precision
        self.use_auth_token = use_auth_token
        self.use_8bit_adam = use_8bit_adam
        self.gradient_checkpointing = gradient_checkpointing
        self.seed = seed
        self.resolution = resolution
        self.center_crop = center_crop

        # Captured at the end of the training.
        self.best_model_path = None

    @property
    def user_images_data_dir(self) -> str:
        return os.path.join(os.getcwd(), "data", 'user_images')

    @property
    def preservation_images_data_dir(self) -> str:
        return os.path.join(os.getcwd(), "data", 'preservation_images')

    def run(self):

        lite = LightningLite(precision=self.precision, devices="auto")

        if self.seed is not None:
            L.seed_everything(self.seed)

        # self.prepare_data(lite)

        # # Load the tokenizer
        tokenizer = CLIPTokenizer.from_pretrained(
            self.pretrained_model_name_or_path,
            subfolder="tokenizer",
            revision=self.revision,
            use_auth_token=self.use_auth_token,
        )

        # # Load models and create wrapper for stable diffusion
        text_encoder = CLIPTextModel.from_pretrained(
            self.pretrained_model_name_or_path,
            subfolder="text_encoder",
            revision=self.revision,
            use_auth_token=self.use_auth_token,
        )
        vae = AutoencoderKL.from_pretrained(
            self.pretrained_model_name_or_path,
            subfolder="vae",
            revision=self.revision,
            use_auth_token=self.use_auth_token,
        )
        unet = UNet2DConditionModel.from_pretrained(
            self.pretrained_model_name_or_path,
            subfolder="unet",
            revision=self.revision,
            use_auth_token=self.use_auth_token,
        )

        vae.requires_grad_(False)

        if self.gradient_checkpointing:
            unet.enable_gradient_checkpointing()

        world_size = int(os.getenv("WORLD_SIZE", 1))

        if self.scale_lr:
            self.learning_rate = (
                self.learning_rate * self.gradient_accumulation_steps * self.train_batch_size * world_size
            )

        # # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
        if self.use_8bit_adam:
            try:
                import bitsandbytes as bnb
            except ImportError:
                raise ImportError(
                    "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
                )

            optimizer_class = bnb.optim.AdamW8bit
        else:
            optimizer_class = torch.optim.AdamW

        params_to_optimize = unet.parameters()
        optimizer = optimizer_class(
            params_to_optimize,
            lr=self.learning_rate,
        )

        noise_scheduler = DDPMScheduler.from_config(self.pretrained_model_name_or_path, subfolder="scheduler")

        train_dataset = DreamBoothDataset(
            instance_data_root=self.user_images_data_dir,
            instance_prompt=self.instance_prompt,
            class_data_root=self.preservation_images_data_dir if self.preservation_prompt else None,
            class_prompt=self.preservation_prompt,
            tokenizer=tokenizer,
            size=self.resolution,
            center_crop=self.center_crop,
        )

        def collate_fn(examples):
            input_ids = [example["instance_prompt_ids"] for example in examples]
            pixel_values = [example["instance_images"] for example in examples]

            # Concat class and instance examples for prior preservation.
            # We do this to avoid doing two forward passes.
            if self.preservation_prompt:
                input_ids += [example["class_prompt_ids"] for example in examples]
                pixel_values += [example["class_images"] for example in examples]

            pixel_values = torch.stack(pixel_values)
            pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

            input_ids = tokenizer.pad(
                {"input_ids": input_ids},
                padding="max_length",
                max_length=tokenizer.model_max_length,
                return_tensors="pt",
            ).input_ids

            batch = {
                "input_ids": input_ids,
                "pixel_values": pixel_values,
            }
            return batch

        train_dataloader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=self.train_batch_size,
            shuffle=True,
            collate_fn=collate_fn,
            num_workers=1,
        )

        # lr_scheduler = get_scheduler(
        #     self.lr_scheduler,
        #     optimizer=optimizer,
        #     num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        #     num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
        # )

        unet, optimizer = lite.setup(unet, optimizer)  # Scale your model / optimizers

        # # Move text_encoder and vae to gpu.
        # # For mixed precision training we cast the text_encoder and vae weights to half-precision
        # # as these models are only used for inference, keeping weights in full precision is not required.
        vae.to(lite.device)

        total_batch_size = self.train_batch_size * world_size * self.gradient_accumulation_steps

        global_step = 0

        unet.train()

        while global_step < (self.max_steps / total_batch_size):
            unet.train()

            for step, batch in enumerate(train_dataloader):
                with accelerator.accumulate(unet):
                    # Convert images to latent space
                    latents = vae.encode(batch["pixel_values"].to(dtype=weight_dtype)).latent_dist.sample()
                    latents = latents * 0.18215

                    # Sample noise that we'll add to the latents
                    noise = torch.randn_like(latents)
                    bsz = latents.shape[0]
                    # Sample a random timestep for each image
                    timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
                    timesteps = timesteps.long()

                    # Add noise to the latents according to the noise magnitude at each timestep
                    # (this is the forward diffusion process)
                    noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                    # Get the text embedding for conditioning
                    encoder_hidden_states = text_encoder(batch["input_ids"])[0]

                    # Predict the noise residual
                    noise_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample

                    if args.with_prior_preservation:
                        # Chunk the noise and noise_pred into two parts and compute the loss on each part separately.
                        noise_pred, noise_pred_prior = torch.chunk(noise_pred, 2, dim=0)
                        noise, noise_prior = torch.chunk(noise, 2, dim=0)

                        # Compute instance loss
                        loss = F.mse_loss(noise_pred.float(), noise.float(), reduction="none").mean([1, 2, 3]).mean()

                        # Compute prior loss
                        prior_loss = F.mse_loss(noise_pred_prior.float(), noise_prior.float(), reduction="mean")

                        # Add the prior loss to the instance loss.
                        loss = loss + args.prior_loss_weight * prior_loss
                    else:
                        loss = F.mse_loss(noise_pred.float(), noise.float(), reduction="mean")

                    accelerator.backward(loss)
                    if accelerator.sync_gradients:
                        params_to_clip = (
                            itertools.chain(unet.parameters(), text_encoder.parameters())
                            if args.train_text_encoder
                            else unet.parameters()
                        )
                        accelerator.clip_grad_norm_(params_to_clip, args.max_grad_norm)
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()

                # Checks if the accelerator has performed an optimization step behind the scenes
                if accelerator.sync_gradients:
                    progress_bar.update(1)
                    global_step += 1

                logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
                progress_bar.set_postfix(**logs)
                accelerator.log(logs, step=global_step)

                if global_step >= args.max_train_steps:
                    break

        #     accelerator.wait_for_everyone()

        # # Create the pipeline using using the trained modules and save it.
        # if accelerator.is_main_process:
        #     pipeline = StableDiffusionPipeline.from_pretrained(
        #         args.pretrained_model_name_or_path,
        #         unet=accelerator.unwrap_model(unet),
        #         text_encoder=accelerator.unwrap_model(text_encoder),
        #         revision=args.revision,
        #     )
        #     pipeline.save_pretrained(args.output_dir)

        #     if args.push_to_hub:
        #         repo.push_to_hub(commit_message="End of training", blocking=False, auto_lfs_prune=True)

        # accelerator.end_training()


    def prepare_data(self, lite):
        if self.preservation_prompt is None:
            return

        if lite.local_rank != 0:
            return

        self._download_images()

        self._generate_preservation_images(lite)

    def _download_images(self):
        """Download the images provided by the user"""

        os.makedirs(self.user_images_data_dir, exist_ok=True)

        L = len(os.listdir(self.user_images_data_dir))

        for idx, image_url in enumerate(self.image_urls):

            r = requests.get(image_url, stream=True)

            if r.status_code == 200:

                path = os.path.join(self.user_images_data_dir, f"{idx + L}.jpg")

                with open(path, 'wb') as f:
                    for chunk in r.iter_content(1024):
                        f.write(chunk)
            else:
                print(f"The image from {image_url} doesn't exist.")


    def _generate_preservation_images(self, lite: LightningLite):

        os.makedirs(self.preservation_images_data_dir, exist_ok=True)

        pipeline = StableDiffusionPipeline.from_pretrained(
            self.pretrained_model_name_or_path,
            revision=self.revision,
            use_auth_token=self.use_auth_token,
            torch_dtype=torch.float32,
        )
        pipeline.enable_attention_slicing()

        user_images = os.path.join(os.getcwd(), "data", 'user_images')

        num_new_images = os.listdir(user_images)

        sample_dataset = PromptDataset(self.preservation_prompt, len(num_new_images))
        sample_dataloader = torch.utils.data.DataLoader(
            sample_dataset, 
            batch_size=2,
        )

        sample_dataloader = lite.setup_dataloaders(sample_dataloader)
        pipeline.to(lite.device)

        L = len(os.listdir(self.user_images_data_dir))

        counter = 0

        for example in sample_dataloader:
            images = pipeline(example["prompt"]).images
            for image in images:
                path = os.path.join(self.preservation_images_data_dir, f"{counter + L}.jpg")
                image.save(path)
                counter += 1

        pipeline = None
        gc.collect()
        del pipeline
        with torch.no_grad():
          torch.cuda.empty_cache()


class DreamBoothFineTuner(LiteMultiNode):

    def __init__(
        self,
        *args,
        cloud_compute = L.CloudCompute("gpu"),
        num_nodes: int = 1, 
        **kwargs
    ):
        super().__init__(
            *args,
            work_cls=_DreamBoothFineTunerWork,
            num_nodes=num_nodes,
            cloud_compute=cloud_compute,
            **kwargs
        )