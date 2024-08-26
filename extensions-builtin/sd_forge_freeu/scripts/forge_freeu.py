import torch
import torch.nn.functional as F
import gradio as gr
from modules import scripts
import logging
import sys
from pathlib import Path

def setup_logging(log_file=None):
    logger = logging.getLogger("FreeU")
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger

logger = setup_logging(log_file=Path("freeu_log.txt"))

def Fourier_filter(x, threshold, scale):
    x_freq = torch.fft.fftn(x.float(), dim=(-2, -1))
    x_freq = torch.fft.fftshift(x_freq, dim=(-2, -1))
    B, C, H, W = x_freq.shape
    mask = torch.ones((B, C, H, W), device=x.device)
    crow, ccol = H // 2, W // 2
    mask[..., crow - threshold:crow + threshold, ccol - threshold:ccol + threshold] = scale
    x_freq = x_freq * mask
    x_freq = torch.fft.ifftshift(x_freq, dim=(-2, -1))
    x_filtered = torch.fft.ifftn(x_freq, dim=(-2, -1)).real
    return x_filtered.to(x.dtype)

def is_flux_model(model):
    return 'flux' in str(type(model)).lower() or hasattr(model, 'flux_attributes')

def apply_freeu_to_flux(h, b1, b2, s1, s2):
    logger.info(f"Applying FreeU to FLUX. Input shape: {h.shape}")
    
    chunks = torch.chunk(h, chunks=4, dim=1)
    processed_chunks = []

    for i, chunk in enumerate(chunks):
        logger.info(f"Processing chunk {i}. Shape: {chunk.shape}")
        chunk = flow_transform(chunk, b1)
        chunk = fourier_filter(chunk, s1)
        chunk = non_linear_transform(chunk, b2, s2)
        processed_chunks.append(chunk)

    h = torch.cat(processed_chunks, dim=1)
    logger.info(f"FreeU applied to FLUX. Output shape: {h.shape}")
    return h

def flow_transform(x, scale):
    return x + scale * torch.tanh(x)

def fourier_filter(x, scale):
    x_freq = torch.fft.fftn(x, dim=(-2, -1))
    x_freq = torch.fft.fftshift(x_freq, dim=(-2, -1))
    B, C, H, W = x_freq.shape
    mask = torch.ones_like(x_freq)
    crow, ccol = H // 2, W // 2
    mask[..., crow-H//4:crow+H//4, ccol-W//4:ccol+W//4] = scale
    x_freq = x_freq * mask
    x_freq = torch.fft.ifftshift(x_freq, dim=(-2, -1))
    return torch.fft.ifftn(x_freq, dim=(-2, -1)).real

def non_linear_transform(x, scale1, scale2):
    return torch.tanh(x * scale1) * scale2

def patch_freeu_v2(unet_patcher, b1, b2, s1, s2):
    logger.info("Entering patch_freeu_v2 function")
    logger.info(f"unet_patcher type: {type(unet_patcher)}")
    
    if is_flux_model(unet_patcher):
        logger.info("FLUX model detected. Applying FLUX-specific FreeU.")
        
        def flux_output_block_patch(h, *args, **kwargs):
            logger.info(f"Entering flux_output_block_patch. Input shape: {h.shape}")
            result = apply_freeu_to_flux(h, b1, b2, s1, s2)
            logger.info(f"Exiting flux_output_block_patch. Output shape: {result.shape}")
            return result, None

        if hasattr(unet_patcher, 'set_model_output_block_patch'):
            unet_patcher.set_model_output_block_patch(flux_output_block_patch)
            logger.info("FLUX-specific FreeU patch applied successfully.")
        else:
            logger.warning("Could not set output block patch for FLUX model. FreeU may not be applied.")
        
        return unet_patcher
    
    if hasattr(unet_patcher, 'model'):
        logger.info("unet_patcher has 'model' attribute")
        if hasattr(unet_patcher.model, 'diffusion_model'):
            logger.info("unet_patcher.model has 'diffusion_model' attribute")
            diffusion_model = unet_patcher.model.diffusion_model
        else:
            logger.info("Using unet_patcher.model as diffusion_model")
            diffusion_model = unet_patcher.model
    else:
        logger.info("Using unet_patcher as diffusion_model")
        diffusion_model = unet_patcher

    logger.info(f"diffusion_model type: {type(diffusion_model)}")
    logger.info(f"diffusion_model attributes: {dir(diffusion_model)}")

    if not hasattr(diffusion_model, 'input_blocks') or not hasattr(diffusion_model, 'output_blocks'):
        logger.warning("Model architecture is not compatible with FreeU. Skipping application.")
        return unet_patcher

    model_channels = getattr(diffusion_model, 'model_channels', 320)
    logger.info(f"model_channels: {model_channels}")

    scale_dict = {model_channels * 4: (b1, s1), model_channels * 2: (b2, s2)}
    logger.info(f"scale_dict: {scale_dict}")

    def output_block_patch(h, hsp, transformer_options):
        scale = scale_dict.get(h.shape[1], None)
        if scale is not None:
            hidden_mean = h.mean(1).unsqueeze(1)
            B = hidden_mean.shape[0]
            hidden_max, _ = torch.max(hidden_mean.view(B, -1), dim=-1, keepdim=True)
            hidden_min, _ = torch.min(hidden_mean.view(B, -1), dim=-1, keepdim=True)
            hidden_mean = (hidden_mean - hidden_min.unsqueeze(2).unsqueeze(3)) / (hidden_max - hidden_min).unsqueeze(2).unsqueeze(3)
            h[:, :h.shape[1] // 2] = h[:, :h.shape[1] // 2] * ((scale[0] - 1) * hidden_mean + 1)
            hsp = Fourier_filter(hsp, threshold=1, scale=scale[1])
        return h, hsp

    m = unet_patcher.clone()
    m.set_model_output_block_patch(output_block_patch)
    return m

class FreeUForForge(scripts.Script):
    sorting_priority = 12

    def title(self):
        return "FreeU Integrated"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, *args, **kwargs):
        with gr.Accordion(open=False, label=self.title(),
                          elem_id="extensions-freeu",
                          elem_classes=["extensions-freeu"]):
            freeu_enabled = gr.Checkbox(label='Enabled', value=False)
            freeu_b1 = gr.Slider(label='B1', minimum=0, maximum=2, step=0.01, value=1.01)
            freeu_b2 = gr.Slider(label='B2', minimum=0, maximum=2, step=0.01, value=1.02)
            freeu_s1 = gr.Slider(label='S1', minimum=0, maximum=4, step=0.01, value=0.99)
            freeu_s2 = gr.Slider(label='S2', minimum=0, maximum=4, step=0.01, value=0.95)

        return freeu_enabled, freeu_b1, freeu_b2, freeu_s1, freeu_s2

    def process_before_every_sampling(self, p, *script_args, **kwargs):
        freeu_enabled, freeu_b1, freeu_b2, freeu_s1, freeu_s2 = script_args

        if not freeu_enabled:
            return

        unet = p.sd_model.forge_objects.unet
        
        logger.info(f"Model type before FreeU: {type(unet)}")

        try:
            unet = patch_freeu_v2(unet, freeu_b1, freeu_b2, freeu_s1, freeu_s2)
            logger.info(f"FreeU applied. Model type after: {type(unet)}")
        except Exception as e:
            logger.error(f"Error in patch_freeu_v2: {str(e)}", exc_info=True)
            return

        p.sd_model.forge_objects.unet = unet

        p.extra_generation_params.update(dict(
            freeu_enabled=freeu_enabled,
            freeu_b1=freeu_b1,
            freeu_b2=freeu_b2,
            freeu_s1=freeu_s1,
            freeu_s2=freeu_s2,
        ))

        logger.info("FreeU parameters applied successfully")
        return
