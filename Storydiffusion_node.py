# !/usr/bin/env python
# -*- coding: UTF-8 -*-
import datetime
import gc
import json

import cv2
import numpy as np
import safetensors
import torch
import copy
import os
import random
from PIL import ImageFont


from safetensors.torch import load_file

from .ip_adapter.attention_processor import IPAttnProcessor2_0
import sys
import re
from .utils.gradio_utils import (
    character_to_dict,
    process_original_prompt,
    get_ref_character,
    cal_attn_mask_xl,
    cal_attn_indice_xl_effcient_memory,
    is_torch2_available,
)
from PIL import Image



if is_torch2_available():
    from .utils.gradio_utils import AttnProcessor2_0 as AttnProcessor
else:
    from .utils.gradio_utils import AttnProcessor
from huggingface_hub import hf_hub_download
from diffusers import (StableDiffusionXLPipeline, DiffusionPipeline, DDIMScheduler, ControlNetModel,
                       KDPM2AncestralDiscreteScheduler, LMSDiscreteScheduler,
                        DPMSolverMultistepScheduler, DPMSolverSinglestepScheduler,
                       EulerDiscreteScheduler, HeunDiscreteScheduler, UNet2DConditionModel,
                       KDPM2DiscreteScheduler,
                       EulerAncestralDiscreteScheduler, UniPCMultistepScheduler, AutoencoderKL,
                       StableDiffusionXLControlNetPipeline, DDPMScheduler, LCMScheduler)
from omegaconf import OmegaConf
from transformers import CLIPVisionModelWithProjection
from transformers import CLIPImageProcessor
from .msdiffusion.models.projection import Resampler
from .msdiffusion.models.model import MSAdapter
from .msdiffusion.utils import get_phrase_idx, get_eot_idx
from diffusers.schedulers.scheduling_ddim import DDIMScheduler
import torch.nn.functional as F
from .utils.utils import get_comic
from .utils.style_template import styles
from .utils.load_models_utils import  load_models, get_instance_path, get_lora_dict
import folder_paths
from comfy.utils import common_upscale

global total_count, attn_count, cur_step, mask1024, mask4096, attn_procs, unet
global sa32, sa64
global write
global height, width
STYLE_NAMES = list(styles.keys())

photomaker_dir=os.path.join(folder_paths.models_dir, "photomaker")

device = (
    "cuda"
    if torch.cuda.is_available()
    else "mps" if torch.backends.mps.is_available() else "cpu"
)

MAX_SEED = np.iinfo(np.int32).max
dir_path = os.path.dirname(os.path.abspath(__file__))
path_dir = os.path.dirname(dir_path)
file_path = os.path.dirname(path_dir)

fonts_path = os.path.join(dir_path, "fonts")
fonts_lists = os.listdir(fonts_path)

lora_get = get_lora_dict()
lora_lightning_list = lora_get["lightning_xl_lora"]
diff_paths = []
for search_path in folder_paths.get_folder_paths("diffusers"):
    if os.path.exists(search_path):
        for root, subdir, files in os.walk(search_path, followlinks=True):
            if "model_index.json" in files:
                diff_paths.append(os.path.relpath(root, start=search_path))

if diff_paths:
    diff_paths = ["none"] + [x for x in diff_paths if x]
else:
    diff_paths = ["none", ]


clip_lists = folder_paths.get_filename_list("clip_vision")
if clip_lists:
    clip_paths = ["none"] + [x for x in clip_lists if x]
else:
    clip_paths = ["none",]


control_paths = []
paths_a = []
for search_path in folder_paths.get_folder_paths("diffusers"):
    if os.path.exists(search_path):
        for root, subdir, files in os.walk(search_path, followlinks=True):
            if "model_index.json" in files:
                control_paths.append(os.path.relpath(root, start=search_path))
            if "config.json" in files:
                paths_a.append(os.path.relpath(root, start=search_path))
                paths_a = ([z for z in paths_a if "controlnet-canny-sdxl-1.0" in z]
                           + [p for p in paths_a if "MistoLine" in p]
                           + [o for o in paths_a if "lcm-sdxl" in o]
                           + [Q for Q in paths_a if "controlnet-openpose-sdxl-1.0" in Q]
                           + [Z for Z in paths_a if "controlnet-scribble-sdxl-1.0" in Z]
                           + [a for a in paths_a if "controlnet-depth-sdxl-1.0" in a]
                           +[b for b in paths_a if "controlnet-tile-sdxl-1.0" in b]
                           +[c for c in paths_a if "controlnet-zoe-depth-sdxl-1.0" in c]
                           +[d for d in paths_a if "sdxl-controlnet-seg " in d])

if control_paths != [] or paths_a != []:
    control_paths = ["none"] + [x for x in control_paths if x] + [y for y in paths_a if y]
else:
    control_paths = ["none", ]

scheduler_list = [
    "Euler", "Euler a", "DDIM", "DDPM", "DPM++ 2M", "DPM++ 2M Karras", "DPM++ 2M SDE", "DPM++ 2M SDE Karras",
    "DPM++ SDE", "DPM++ SDE Karras", "DPM2",
    "DPM2 Karras", "DPM2 a", "DPM2 a Karras", "Heun", "LCM", "LMS", "LMS Karras", "UniPC"
]

def phi2narry(img):
    img = torch.from_numpy(np.array(img).astype(np.float32) / 255.0).unsqueeze(0)
    return img


def get_instance_path(path):
    instance_path = os.path.normpath(path)
    if sys.platform == 'win32':
        instance_path = instance_path.replace('\\', "/")
    return instance_path

def add_pil(list, list_add, num):
    new_list = list[:num] + list_add + list[num:]
    return new_list

def tensor_to_image(tensor):
    image_np = tensor.squeeze().mul(255).clamp(0, 255).byte().numpy()
    image = Image.fromarray(image_np, mode='RGB')
    return image

# get fonts list
def has_parentheses(s):
    return bool(re.search(r'\(.*?\)', s))

def contains_brackets(s):
    return '[' in s or ']' in s

def extract_content_from_brackets(text):
    # 正则表达式匹配多对方括号内的内容
    return re.findall(r'\[(.*?)\]', text)

def narry_list(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        modified_value = phi2narry(value)
        list_in[i] = modified_value
    return list_in


def remove_punctuation_from_strings(lst):
    pattern = r"[\W]+$"  # 匹配字符串末尾的所有非单词字符
    return [re.sub(pattern, '', s) for s in lst]


def format_punctuation_from_strings(lst):
    pattern = r"[\W]+$"  # 匹配字符串末尾的所有非单词字符
    return [re.sub(pattern, ';', s) for s in lst]


def phi_list(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        list_in[i] = value
    return list_in


def narry_list_pil(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        modified_value = tensor_to_image(value)
        list_in[i] = modified_value
    return list_in


def get_local_path(file_path, model_path):
    path = os.path.join(file_path, "models", "diffusers", model_path)
    model_path = os.path.normpath(path)
    if sys.platform.startswith('win32'):
        model_path = model_path.replace('\\', "/")
    return model_path


def setup_seed(seed):
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


def get_image_path_list(folder_name):
    image_basename_list = os.listdir(folder_name)
    image_path_list = sorted(
        [os.path.join(folder_name, basename) for basename in image_basename_list]
    )
    return image_path_list


def apply_style_positive(style_name: str, positive: str):
    p, n = styles.get(style_name, styles[style_name])
    #print(p, "test0", n)
    return p.replace("{prompt}", positive),n


def apply_style(style_name: str, positives: list, negative: str = ""):
    p, n = styles.get(style_name, styles[style_name])
    #print(p,"test1",n)
    return [
        p.replace("{prompt}", positive) for positive in positives
    ], n + " " + negative


def load_character_files(character_files: str):
    if character_files == "":
        raise "Please set a character file!"
    character_files_arr = character_files.splitlines()
    primarytext = []
    for character_file_name in character_files_arr:
        character_file = torch.load(
            character_file_name, map_location=torch.device("cpu")
        )
        character_file.eval()
        primarytext.append(character_file["character"] + character_file["description"])
    return array2string(primarytext)


def array2string(arr):
    stringtmp = ""
    for i, part in enumerate(arr):
        if i != len(arr) - 1:
            stringtmp += part + "\n"
        else:
            stringtmp += part

    return stringtmp


def find_directories(base_path):
    directories = []
    for root, dirs, files in os.walk(base_path):
        for name in dirs:
            directories.append(name)
    return directories


base_pt = os.path.join(photomaker_dir,"pt")
if not os.path.exists(base_pt):
    os.makedirs(base_pt)
pt_path_list = find_directories(base_pt)
if pt_path_list:
    character_weights=["none"]+pt_path_list
else:
    character_weights=["none",]


def get_scheduler(name):
    scheduler = False
    if name == "Euler":
        scheduler = EulerDiscreteScheduler()
    elif name == "Euler a":
        scheduler = EulerAncestralDiscreteScheduler()
    elif name == "DDIM":
        scheduler = DDIMScheduler()
    elif name == "DDPM":
        scheduler = DDPMScheduler()
    elif name == "DPM++ 2M":
        scheduler = DPMSolverMultistepScheduler()
    elif name == "DPM++ 2M Karras":
        scheduler = DPMSolverMultistepScheduler(use_karras_sigmas=True)
    elif name == "DPM++ 2M SDE":
        scheduler = DPMSolverMultistepScheduler(algorithm_type="sde-dpmsolver++")
    elif name == "DPM++ 2M SDE Karras":
        scheduler = DPMSolverMultistepScheduler(use_karras_sigmas=True, algorithm_type="sde-dpmsolver++")
    elif name == "DPM++ SDE":
        scheduler = DPMSolverSinglestepScheduler()
    elif name == "DPM++ SDE Karras":
        scheduler = DPMSolverSinglestepScheduler(use_karras_sigmas=True)
    elif name == "DPM2":
        scheduler = KDPM2DiscreteScheduler()
    elif name == "DPM2 Karras":
        scheduler = KDPM2DiscreteScheduler(use_karras_sigmas=True)
    elif name == "DPM2 a":
        scheduler = KDPM2AncestralDiscreteScheduler()
    elif name == "DPM2 a Karras":
        scheduler = KDPM2AncestralDiscreteScheduler(use_karras_sigmas=True)
    elif name == "Heun":
        scheduler = HeunDiscreteScheduler()
    elif name == "LCM":
        scheduler = LCMScheduler()
    elif name == "LMS":
        scheduler = LMSDiscreteScheduler()
    elif name == "LMS Karras":
        scheduler = LMSDiscreteScheduler(use_karras_sigmas=True)
    elif name == "UniPC":
        scheduler = UniPCMultistepScheduler()
    return scheduler


def set_attention_processor(unet, id_length, is_ipadapter=False):
    global attn_procs
    attn_procs = {}
    for name in unet.attn_processors.keys():
        cross_attention_dim = (
            None
            if name.endswith("attn1.processor")
            else unet.config.cross_attention_dim
        )
        if name.startswith("mid_block"):
            hidden_size = unet.config.block_out_channels[-1]
        elif name.startswith("up_blocks"):
            block_id = int(name[len("up_blocks.")])
            hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
        elif name.startswith("down_blocks"):
            block_id = int(name[len("down_blocks.")])
            hidden_size = unet.config.block_out_channels[block_id]
        if cross_attention_dim is None:
            if name.startswith("up_blocks"):
                attn_procs[name] = SpatialAttnProcessor2_0(id_length=id_length)
            else:
                attn_procs[name] = AttnProcessor()
        else:
            if is_ipadapter:
                attn_procs[name] = IPAttnProcessor2_0(
                    hidden_size=hidden_size,
                    cross_attention_dim=cross_attention_dim,
                    scale=1,
                    num_tokens=4,
                ).to(unet.device, dtype=torch.float16)
            else:
                attn_procs[name] = AttnProcessor()

    unet.set_attn_processor(copy.deepcopy(attn_procs))

def load_single_character_weights(unet, filepath):
    """
    从指定文件中加载权重到 attention_processor 类的 id_bank 中。
    参数:
    - model: 包含 attention_processor 类实例的模型。
    - filepath: 权重文件的路径。
    """
    # 使用torch.load来读取权重
    weights_to_load = torch.load(filepath, map_location=torch.device("cpu"))
    weights_to_load.eval()
    character = weights_to_load["character"]
    description = weights_to_load["description"]
    #print(character)
    for attn_name, attn_processor in unet.attn_processors.items():
        if isinstance(attn_processor, SpatialAttnProcessor2_0):
            # 转移权重到GPU（如果GPU可用的话）并赋值给id_bank
            attn_processor.id_bank[character] = {}
            for step_key in weights_to_load[attn_name].keys():

                attn_processor.id_bank[character][step_key] = [
                    tensor.to(unet.device)
                    for tensor in weights_to_load[attn_name][step_key]
                ]
    print("successsfully,load_single_character_weights")


def load_character_files_on_running(unet, character_files: str):
    if character_files == "":
        return False
    weights_list = os.listdir(character_files)#获取路径下的权重列表
    #character_files_arr = character_files.splitlines()
    for character_file in weights_list:
        path_cur=os.path.join(character_files,character_file)
        load_single_character_weights(unet, path_cur)
    return True


def save_single_character_weights(unet, character, description, filepath):
    """
    保存 attention_processor 类中的 id_bank GPU Tensor 列表到指定文件中。
    参数:
    - model: 包含 attention_processor 类实例的模型。
    - filepath: 权重要保存到的文件路径。
    """
    weights_to_save = {}
    weights_to_save["description"] = description
    weights_to_save["character"] = character
    for attn_name, attn_processor in unet.attn_processors.items():
        if isinstance(attn_processor, SpatialAttnProcessor2_0):
            # 将每个 Tensor 转到 CPU 并转为列表，以确保它可以被序列化
            #print(attn_name, attn_processor)
            weights_to_save[attn_name] = {}
            for step_key in attn_processor.id_bank[character].keys():
                weights_to_save[attn_name][step_key] = [
                    tensor.cpu()
                    for tensor in attn_processor.id_bank[character][step_key]
                ]
    # 使用torch.save保存权重
    torch.save(weights_to_save, filepath)



def save_results(unet):
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    weight_folder_name =os.path.join(base_pt,f"{timestamp}")
    #创建文件夹
    if not os.path.exists(weight_folder_name):
        os.makedirs(weight_folder_name)
    global character_dict
    for char in character_dict:
        description = character_dict[char]
        save_single_character_weights(unet,char,description,os.path.join(weight_folder_name, f'{char}.pt'))


class SpatialAttnProcessor2_0(torch.nn.Module):
    r"""
    Attention processor for IP-Adapater for PyTorch 2.0.
    Args:
        hidden_size (`int`):
            The hidden size of the attention layer.
        cross_attention_dim (`int`):
            The number of channels in the `encoder_hidden_states`.
        text_context_len (`int`, defaults to 77):
            The context length of the text features.
        scale (`float`, defaults to 1.0):
            the weight scale of image prompt.
    """

    def __init__(
            self,
            hidden_size=None,
            cross_attention_dim=None,
            id_length=4,
            device=device,
            dtype=torch.float16,
    ):
        super().__init__()
        if not hasattr(F, "scaled_dot_product_attention"):
            raise ImportError(
                "AttnProcessor2_0 requires PyTorch 2.0, to use it, please upgrade PyTorch to 2.0."
            )
        self.device = device
        self.dtype = dtype
        self.hidden_size = hidden_size
        self.cross_attention_dim = cross_attention_dim
        self.total_length = id_length + 1
        self.id_length = id_length
        self.id_bank = {}

    def __call__(
            self,
            attn,
            hidden_states,
            encoder_hidden_states=None,
            attention_mask=None,
            temb=None,
    ):
        # un_cond_hidden_states, cond_hidden_states = hidden_states.chunk(2)
        # un_cond_hidden_states = self.__call2__(attn, un_cond_hidden_states,encoder_hidden_states,attention_mask,temb)
        # 生成一个0到1之间的随机数
        global total_count, attn_count, cur_step, indices1024, indices4096
        global sa32, sa64
        global write
        global height, width
        global character_dict
        global  character_index_dict, invert_character_index_dict, cur_character, ref_indexs_dict, ref_totals, cur_character
        if attn_count == 0 and cur_step == 0:
            indices1024, indices4096 = cal_attn_indice_xl_effcient_memory(
                self.total_length,
                self.id_length,
                sa32,
                sa64,
                height,
                width,
                device=self.device,
                dtype=self.dtype,
            )
        if write:
            assert len(cur_character) == 1
            if hidden_states.shape[1] == (height // 32) * (width // 32):
                indices = indices1024
            else:
                indices = indices4096
            # print(f"white:{cur_step}")
            total_batch_size, nums_token, channel = hidden_states.shape
            img_nums = total_batch_size // 2
            hidden_states = hidden_states.reshape(-1, img_nums, nums_token, channel)
            # print(img_nums,len(indices),hidden_states.shape,self.total_length)
            if cur_character[0] not in self.id_bank:
                self.id_bank[cur_character[0]] = {}
            self.id_bank[cur_character[0]][cur_step] = [
                hidden_states[:, img_ind, indices[img_ind], :]
                .reshape(2, -1, channel)
                .clone()
                for img_ind in range(img_nums)
            ]
            hidden_states = hidden_states.reshape(-1, nums_token, channel)
            # self.id_bank[cur_step] = [hidden_states[:self.id_length].clone(), hidden_states[self.id_length:].clone()]
        else:
            # encoder_hidden_states = torch.cat((self.id_bank[cur_step][0].to(self.device),self.id_bank[cur_step][1].to(self.device)))
            # TODO: ADD Multipersion Control
            encoder_arr = []
            for character in cur_character:
                encoder_arr = encoder_arr + [
                    tensor.to(self.device)
                    for tensor in self.id_bank[character][cur_step]
                ]
        # 判断随机数是否大于0.5
        if cur_step < 1:
            hidden_states = self.__call2__(
                attn, hidden_states, None, attention_mask, temb
            )
        else:  # 256 1024 4096
            random_number = random.random()
            if cur_step < 20:
                rand_num = 0.3
            else:
                rand_num = 0.1
            # print(f"hidden state shape {hidden_states.shape[1]}")
            if random_number > rand_num:
                if hidden_states.shape[1] == (height // 32) * (width // 32):
                    indices = indices1024
                else:
                    indices = indices4096
                # print("before attention",hidden_states.shape,attention_mask.shape,encoder_hidden_states.shape if encoder_hidden_states is not None else "None")
                if write:
                    total_batch_size, nums_token, channel = hidden_states.shape
                    img_nums = total_batch_size // 2
                    hidden_states = hidden_states.reshape(
                        -1, img_nums, nums_token, channel
                    )
                    encoder_arr = [
                        hidden_states[:, img_ind, indices[img_ind], :].reshape(
                            2, -1, channel
                        )
                        for img_ind in range(img_nums)
                    ]
                    for img_ind in range(img_nums):
                        # print(img_nums)
                        # assert img_nums != 1
                        img_ind_list = [i for i in range(img_nums)]
                        # print(img_ind_list,img_ind)
                        img_ind_list.remove(img_ind)
                        # print(img_ind,invert_character_index_dict[img_ind])
                        # print(character_index_dict[invert_character_index_dict[img_ind]])
                        # print(img_ind_list)
                        # print(img_ind,img_ind_list)
                        encoder_hidden_states_tmp = torch.cat(
                            [encoder_arr[img_ind] for img_ind in img_ind_list]
                            + [hidden_states[:, img_ind, :, :]],
                            dim=1,
                        )

                        hidden_states[:, img_ind, :, :] = self.__call2__(
                            attn,
                            hidden_states[:, img_ind, :, :],
                            encoder_hidden_states_tmp,
                            None,
                            temb,
                        )
                else:
                    _, nums_token, channel = hidden_states.shape
                    # img_nums = total_batch_size // 2
                    # encoder_hidden_states = encoder_hidden_states.reshape(-1,img_nums,nums_token,channel)
                    hidden_states = hidden_states.reshape(2, -1, nums_token, channel)

                    # encoder_arr = [encoder_hidden_states[:,img_ind,indices[img_ind],:].reshape(2,-1,channel) for img_ind in range(img_nums)]
                    encoder_hidden_states_tmp = torch.cat(
                        encoder_arr + [hidden_states[:, 0, :, :]], dim=1
                    )

                    hidden_states[:, 0, :, :] = self.__call2__(
                        attn,
                        hidden_states[:, 0, :, :],
                        encoder_hidden_states_tmp,
                        None,
                        temb,
                    )
                hidden_states = hidden_states.reshape(-1, nums_token, channel)
            else:
                hidden_states = self.__call2__(
                    attn, hidden_states, None, attention_mask, temb
                )
        attn_count += 1
        if attn_count == total_count:
            attn_count = 0
            cur_step += 1
            indices1024, indices4096 = cal_attn_indice_xl_effcient_memory(
                self.total_length,
                self.id_length,
                sa32,
                sa64,
                height,
                width,
                device=self.device,
                dtype=self.dtype,
            )

        return hidden_states

    def __call2__(
            self,
            attn,
            hidden_states,
            encoder_hidden_states=None,
            attention_mask=None,
            temb=None,
    ):
        residual = hidden_states

        if attn.spatial_norm is not None:
            hidden_states = attn.spatial_norm(hidden_states, temb)

        input_ndim = hidden_states.ndim

        if input_ndim == 4:
            batch_size, channel, height, width = hidden_states.shape
            hidden_states = hidden_states.view(
                batch_size, channel, height * width
            ).transpose(1, 2)

        batch_size, sequence_length, channel = hidden_states.shape

        if attention_mask is not None:
            attention_mask = attn.prepare_attention_mask(
                attention_mask, sequence_length, batch_size
            )
            # scaled_dot_product_attention expects attention_mask shape to be
            # (batch, heads, source_length, target_length)
            attention_mask = attention_mask.view(
                batch_size, attn.heads, -1, attention_mask.shape[-1]
            )

        if attn.group_norm is not None:
            hidden_states = attn.group_norm(hidden_states.transpose(1, 2)).transpose(
                1, 2
            )

        query = attn.to_q(hidden_states)

        if encoder_hidden_states is None:
            encoder_hidden_states = hidden_states  # B, N, C
        # else:
        #     encoder_hidden_states = encoder_hidden_states.view(-1,self.id_length+1,sequence_length,channel).reshape(-1,(self.id_length+1) * sequence_length,channel)

        key = attn.to_k(encoder_hidden_states)
        value = attn.to_v(encoder_hidden_states)

        inner_dim = key.shape[-1]
        head_dim = inner_dim // attn.heads

        query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        # the output of sdp = (batch, num_heads, seq_len, head_dim)
        # TODO: add support for attn.scale when we move to Torch 2.1
        hidden_states = F.scaled_dot_product_attention(
            query, key, value, attn_mask=attention_mask, dropout_p=0.0, is_causal=False
        )

        hidden_states = hidden_states.transpose(1, 2).reshape(
            batch_size, -1, attn.heads * head_dim
        )
        hidden_states = hidden_states.to(query.dtype)

        # linear proj
        hidden_states = attn.to_out[0](hidden_states)
        # dropout
        hidden_states = attn.to_out[1](hidden_states)

        if input_ndim == 4:
            hidden_states = hidden_states.transpose(-1, -2).reshape(
                batch_size, channel, height, width
            )

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        hidden_states = hidden_states / attn.rescale_output_factor

        return hidden_states


def process_generation(
        pipe,
        upload_images,
        model_type,
        _num_steps,
        style_name,
        _Ip_Adapter_Strength,
        _style_strength_ratio,
        guidance_scale,
        seed_,
        id_length,
        general_prompt,
        negative_prompt,
        prompt_array,
        width,
        height,
        load_chars,
        lora,
        trigger_words, photomake_mode, use_kolor, use_flux, auraface
):  # Corrected font_choice usage

    if len(general_prompt.splitlines()) >= 3:
        raise "Support for more than three characters is temporarily unavailable due to VRAM limitations, but this issue will be resolved soon."
    # _model_type = "Photomaker" if _model_type == "Using Ref Images" else "original"

    if not use_kolor and not use_flux:
        if model_type == "img2img" and "img" not in general_prompt:
            raise 'Please choice img2img typle,and add the triger word " img "  behind the class word you want to customize, such as: man img or woman img'

    global total_length, attn_procs, cur_model_type
    global write
    global cur_step, attn_count

    # load_chars = load_character_files_on_running(unet, character_files=char_files)

    prompts_origin = prompt_array.splitlines()
    prompts = [prompt for prompt in prompts_origin if not len(extract_content_from_brackets(prompt)) >= 2]  # 剔除双角色
    if use_kolor:
        add_trigger_words = "," + trigger_words + " " + "风格" + ";"
    else:
        add_trigger_words = "," + trigger_words + " " + "style" + ";"

    if lora:
        if lora in lora_lightning_list:
            prompts = remove_punctuation_from_strings(prompts)
        else:
            prompts = remove_punctuation_from_strings(prompts)
            prompts = [item + add_trigger_words for item in prompts]

    global character_index_dict, invert_character_index_dict, ref_indexs_dict, ref_totals
    global character_dict

    character_dict, character_list = character_to_dict(general_prompt, lora, add_trigger_words)
    # print(character_dict)
    start_merge_step = int(float(_style_strength_ratio) / 100 * _num_steps)
    if start_merge_step > 30:
        start_merge_step = 30
    print(f"start_merge_step:{start_merge_step}")
    generator = torch.Generator(device=device).manual_seed(seed_)
    clipped_prompts = prompts[:]
    nc_indexs = []
    for ind, prompt in enumerate(clipped_prompts):
        if "[NC]" in prompt:
            nc_indexs.append(ind)
            if ind < id_length:
                raise f"The first {id_length} row is id prompts, cannot use [NC]!"

    prompts = [
        prompt if "[NC]" not in prompt else prompt.replace("[NC]", "")
        for prompt in clipped_prompts
    ]

    if lora:
        if lora in lora_lightning_list:
            prompts = [
                prompt.rpartition("#")[0] if "#" in prompt else prompt for prompt in prompts
            ]
        else:
            prompts = [
                prompt.rpartition("#")[0] + add_trigger_words if "#" in prompt else prompt for prompt in prompts
            ]
    else:
        prompts = [
            prompt.rpartition("#")[0] if "#" in prompt else prompt for prompt in prompts
        ]

    # id_prompts = prompts[:id_length]
    (
        character_index_dict,
        invert_character_index_dict,
        replace_prompts,
        ref_indexs_dict,
        ref_totals,
    ) = process_original_prompt(character_dict, prompts, id_length)

    if model_type == "img2img":
        # _upload_images = [_upload_images]
        input_id_images_dict = {}

        if len(upload_images) != len(character_dict.keys()):
            raise f"You upload images({len(upload_images)}) is not equal to the number of characters({len(character_dict.keys())})!"

        for ind, img in enumerate(upload_images):
            input_id_images_dict[character_list[ind]] = [img]  # 已经pil转化了 不用load
            # input_id_images_dict[character_list[ind]] = [load_image(img)]
    # real_prompts = prompts[id_length:]
    # if device == "cuda":
    #     torch.cuda.empty_cache()
    write = True
    cur_step = 0
    attn_count = 0
    # id_prompts_no_using, negative_prompt = apply_style(style_name, ["id_prompts"], negative_prompt)
    # setup_seed(seed_)
    total_results = []
    id_images = []
    results_dict = {}
    p_num = 0
    if photomake_mode == "v2":
        from .utils.insightface_package import FaceAnalysis2, analyze_faces
        if auraface:
            from huggingface_hub import snapshot_download
            snapshot_download(
                "fal/AuraFace-v1",
                local_dir="models/auraface",
            )
            face_detector = FaceAnalysis2(name="auraface",providers=["CUDAExecutionProvider", "CPUExecutionProvider"],root=".",
                                          allowed_modules=['detection', 'recognition'])
        else:
            face_detector = FaceAnalysis2(providers=['CUDAExecutionProvider'],
                                          allowed_modules=['detection', 'recognition'])
        face_detector.prepare(ctx_id=0, det_size=(640, 640))

    global cur_character
    if not load_chars:
        for character_key in character_dict.keys():  # 先生成角色对应第一句场景提示词的图片
            cur_character = [character_key]
            ref_indexs = ref_indexs_dict[character_key]
            current_prompts = [replace_prompts[ref_ind] for ref_ind in ref_indexs]
            setup_seed(seed_)
            generator = torch.Generator(device=device).manual_seed(seed_)
            cur_step = 0
            cur_positive_prompts, cur_negative_prompt = apply_style(
                style_name, current_prompts, negative_prompt
            )

            if use_kolor:
                cur_negative_prompt = [cur_negative_prompt]

            if model_type == "txt2img":
                if use_flux:
                    id_images = pipe(
                        prompt=cur_positive_prompts,
                        num_inference_steps=_num_steps,
                        guidance_scale=guidance_scale,
                        output_type="pil",
                        max_sequence_length=256,
                        height=height,
                        width=width,
                        generator=torch.Generator("cpu").manual_seed(0)
                    ).images
                else:
                    if use_kolor:
                        cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len( cur_negative_prompt) != len(
                            cur_positive_prompts) else cur_negative_prompt
                    id_images = pipe(
                        cur_positive_prompts,
                        num_inference_steps=_num_steps,
                        guidance_scale=guidance_scale,
                        height=height,
                        width=width,
                        negative_prompt=cur_negative_prompt,
                        generator=generator
                    ).images

            elif model_type == "img2img":
                if use_kolor:
                    pipe.set_ip_adapter_scale([_Ip_Adapter_Strength])
                    cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(cur_negative_prompt)!=len(cur_positive_prompts) else  cur_negative_prompt
                    id_images = pipe(
                        prompt=cur_positive_prompts,
                        ip_adapter_image=input_id_images_dict[character_key],
                        negative_prompt=cur_negative_prompt,
                        num_inference_steps=_num_steps,
                        height=height,
                        width=width,
                        guidance_scale=guidance_scale,
                        num_images_per_prompt=1,
                        generator=generator,
                    ).images
                elif use_flux:
                    id_images = pipe(
                        prompt=cur_positive_prompts,
                        latents=None,
                        num_inference_steps=_num_steps,
                        height=height,
                        width=width,
                        output_type="pil",
                        max_sequence_length=256,
                        guidance_scale=guidance_scale,
                        generator=torch.Generator("cpu").manual_seed(0),
                    ).images
                else:
                    if photomake_mode == "v2":
                        # 提取id
                        # print("v2 mode load_chars", cur_positive_prompts, negative_prompt, character_key)
                        img = input_id_images_dict[character_key][
                            0]  # input_id_images_dict {'[Taylor]': [pil], '[Lecun]': [pil]}
                        img = np.array(img)
                        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

                        faces = analyze_faces(face_detector, img, )
                        id_embed_list = [torch.from_numpy((faces[0]['embedding']))]
                        id_embeds = torch.stack(id_embed_list)

                        id_images = pipe(
                            cur_positive_prompts,
                            input_id_images=input_id_images_dict[character_key],
                            num_inference_steps=_num_steps,
                            guidance_scale=guidance_scale,
                            start_merge_step=start_merge_step,
                            height=height,
                            width=width,
                            negative_prompt= cur_negative_prompt,
                            id_embeds=id_embeds,
                            generator=generator
                        ).images
                    else:
                        # print("v1 mode,load_chars", cur_positive_prompts, negative_prompt,character_key )
                        id_images = pipe(
                            cur_positive_prompts,
                            input_id_images=input_id_images_dict[character_key],
                            num_inference_steps=_num_steps,
                            guidance_scale=guidance_scale,
                            start_merge_step=start_merge_step,
                            height=height,
                            width=width,
                            negative_prompt= cur_negative_prompt,
                            generator=generator
                        ).images
            else:
                raise NotImplementedError(
                    "You should choice between original and Photomaker!",
                    f"But you choice {model_type}",
                )
            p_num += 1
            # total_results = id_images + total_results
            # yield total_results
            for ind, img in enumerate(id_images):
                results_dict[ref_indexs[ind]] = img
            # real_images = []
            yield [results_dict[ind] for ind in results_dict.keys()]

    write = False
    if not load_chars:
        real_prompts_inds = [
            ind for ind in range(len(prompts)) if ind not in ref_totals
        ]
    else:
        real_prompts_inds = [ind for ind in range(len(prompts))]

    # print(real_prompts_inds)
    real_prompt_no, negative_prompt_style = apply_style_positive(style_name, "real_prompt")
    negative_prompt = str(negative_prompt) + str(negative_prompt_style)

    for real_prompts_ind in real_prompts_inds:  # 非角色流程
        real_prompt = replace_prompts[real_prompts_ind]
        cur_character = get_ref_character(prompts[real_prompts_ind], character_dict)
        # print(cur_character)
        setup_seed(seed_)

        if len(cur_character) > 1 and model_type == "img2img":
            raise Exception("Temporarily Not Support Multiple character in Ref Image Mode!")

        generator = torch.Generator(device=device).manual_seed(seed_)
        cur_step = 0
        real_prompt, negative_prompt_style_no = apply_style_positive(style_name, real_prompt)
        # print(real_prompt)
        if model_type == "txt2img":
            # print(results_dict,real_prompts_ind)
            if use_flux:
                results_dict[real_prompts_ind] = pipe(
                    prompt=real_prompt,
                    num_inference_steps=_num_steps,
                    guidance_scale=guidance_scale,
                    output_type="pil",
                    max_sequence_length=256,
                    height=height,
                    width=width,
                    generator=torch.Generator("cpu").manual_seed(seed_)
                ).images[0]
            else:
                results_dict[real_prompts_ind] = pipe(
                    real_prompt,
                    num_inference_steps=_num_steps,
                    guidance_scale=guidance_scale,
                    height=height,
                    width=width,
                    negative_prompt=negative_prompt,
                    generator=generator,
                ).images[0]

        elif model_type == "img2img":
            if use_kolor:
                empty_img=Image.new('RGB', (height, width), (255, 255, 255))
                results_dict[real_prompts_ind] = pipe(
                prompt = real_prompt,
                ip_adapter_image = [(
                        input_id_images_dict[cur_character[0]]
                        if real_prompts_ind not in nc_indexs
                        else empty_img
                    ),],
                negative_prompt = negative_prompt,
                height = height,
                width = width,
                num_inference_steps = _num_steps,
                guidance_scale = guidance_scale,
                num_images_per_prompt = 1,
                generator = generator,
                nc_flag=True if real_prompts_ind in nc_indexs else False,  # nc_flag，用索引标记，主要控制非角色人物的生成，默认false
                ).images[0]
            elif use_flux:
                results_dict[real_prompts_ind]=pipe(
                    prompt=real_prompt,
                    latents=None,
                    num_inference_steps=_num_steps,
                    height=height,
                    width=width,
                    output_type="pil",
                    max_sequence_length=256,
                    guidance_scale=guidance_scale,
                    generator=torch.Generator("cpu").manual_seed(seed_),
                ).images[0]
            else:
                if photomake_mode == "v2":
                    # V2版本必须要有id_embeds，只能用input_id_images作为风格参考
                    img = (
                        input_id_images_dict[cur_character[0]]
                        if real_prompts_ind not in nc_indexs
                        else input_id_images_dict[character_list[0]]
                    ),
                    # print(img,type(img[0][0]))

                    img = np.array(img[0][0])
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                    faces = analyze_faces(face_detector, img, )
                    nc_flag = True if real_prompts_ind in nc_indexs else False  # 有nc为ture，无则false
                    if not nc_flag:
                        id_embed_list = [torch.from_numpy((faces[0]['embedding']))]
                    else:
                        id_embed_list = [torch.zeros_like(torch.from_numpy((faces[0]['embedding'])))]
                    id_embeds = torch.stack(id_embed_list)

                    # print(real_prompts_ind, real_prompt,"v2 mode")

                    results_dict[real_prompts_ind] = pipe(
                        real_prompt,
                        input_id_images=(
                            input_id_images_dict[cur_character[0]]
                            if real_prompts_ind not in nc_indexs
                            else input_id_images_dict[character_list[0]]
                        ),
                        num_inference_steps=_num_steps,
                        guidance_scale=guidance_scale,
                        start_merge_step=start_merge_step,
                        height=height,
                        width=width,
                        negative_prompt=negative_prompt,
                        generator=generator,
                        id_embeds=id_embeds,
                        nc_flag=True if real_prompts_ind in nc_indexs else False,
                    ).images[0]
                else:
                    # print(real_prompts_ind, real_prompt, "v1 mode", )
                    results_dict[real_prompts_ind] = pipe(
                        real_prompt,
                        input_id_images=(
                            input_id_images_dict[cur_character[0]]
                            if real_prompts_ind not in nc_indexs
                            else input_id_images_dict[character_list[0]]
                        ),
                        num_inference_steps=_num_steps,
                        guidance_scale=guidance_scale,
                        start_merge_step=start_merge_step,
                        height=height,
                        width=width,
                        negative_prompt=negative_prompt,
                        generator=generator,
                        nc_flag=True if real_prompts_ind in nc_indexs else False,  # nc_flag，用索引标记，主要控制非角色人物的生成，默认false
                    ).images[0]
        else:
            raise NotImplementedError(
                "You should choice between original and Photomaker!",
                f"But you choice {model_type}",
            )
        yield [results_dict[ind] for ind in results_dict.keys()]

    total_results = [results_dict[ind] for ind in range(len(prompts))]
    torch.cuda.empty_cache()
    yield total_results


def nomarl_tensor_upscale(tensor, width, height):
    samples = tensor.movedim(-1, 1)
    samples = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = samples.movedim(1, -1)
    return samples

def nomarl_upscale(img, width, height):
    samples = img.movedim(-1, 1)
    img = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = img.movedim(1, -1)
    img = tensor_to_image(samples)
    return img
def nomarl_upscale_tensor(img, width, height):
    samples = img.movedim(-1, 1)
    img = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = img.movedim(1, -1)
    return samples

def get_phrases_idx(tokenizer, phrases, prompt):
    res = []
    phrase_cnt = {}
    for phrase in phrases:
        if phrase in phrase_cnt:
            cur_cnt = phrase_cnt[phrase]
            phrase_cnt[phrase] += 1
        else:
            cur_cnt = 0
            phrase_cnt[phrase] = 1
        res.append(get_phrase_idx(tokenizer, phrase, prompt, num=cur_cnt)[0])
    return res


def main_normal(prompt,pipe,phrases,ms_model,input_images,num_samples,steps,seed,negative_prompt,scale,image_encoder,cfg,image_processor,
                boxes,mask_threshold,start_step,image_proj_type,image_encoder_type,drop_grounding_tokens,height,width,phrase_idxes, eot_idxes,in_img,use_repo):
    if use_repo:
        in_img = None
    images = ms_model.generate(pipe=pipe, pil_images=[input_images],processed_images=in_img, num_samples=num_samples,
                               num_inference_steps=steps,
                               seed=seed,
                               prompt=[prompt], negative_prompt=negative_prompt, scale=scale,
                               image_encoder=image_encoder, guidance_scale=cfg,
                               image_processor=image_processor, boxes=boxes,
                               mask_threshold=mask_threshold,
                               start_step=start_step,
                               image_proj_type=image_proj_type,
                               image_encoder_type=image_encoder_type,
                               phrases=phrases,
                               drop_grounding_tokens=drop_grounding_tokens,
                               phrase_idxes=phrase_idxes, eot_idxes=eot_idxes, height=height,
                               width=width)
    return images
def main_control(prompt,width,height,pipe,phrases,ms_model,input_images,num_samples,steps,seed,negative_prompt,scale,image_encoder,cfg,
                 image_processor,boxes,mask_threshold,start_step,image_proj_type,image_encoder_type,drop_grounding_tokens,controlnet_scale,control_image,phrase_idxes, eot_idxes,in_img,use_repo):
    if use_repo:
        in_img=None
    images = ms_model.generate(pipe=pipe, pil_images=[input_images],processed_images=in_img, num_samples=num_samples,
                               num_inference_steps=steps,
                               seed=seed,
                               prompt=[prompt], negative_prompt=negative_prompt, scale=scale,
                               image_encoder=image_encoder, guidance_scale=cfg,
                               image_processor=image_processor, boxes=boxes,
                               mask_threshold=mask_threshold,
                               start_step=start_step,
                               image_proj_type=image_proj_type,
                               image_encoder_type=image_encoder_type,
                               phrases=phrases,
                               drop_grounding_tokens=drop_grounding_tokens,
                               phrase_idxes=phrase_idxes, eot_idxes=eot_idxes, height=height,
                               width=width,
                               image=control_image, controlnet_conditioning_scale=controlnet_scale)

    return images

def get_float(str_in):
    list_str=str_in.split(",")
    float_box=[float(x) for x in list_str]
    return float_box

def msdiffusion_main(pipe, image_1, image_2, prompts_dual, width, height, steps, seed, style_name,char_describe,char_origin,negative_prompt,
                     clip_vision, _model_type, lora, lora_path, lora_scale, trigger_words, ckpt_path,dif_repo,
                      role_scale, mask_threshold, start_step,controlnet_model_path,control_image,controlnet_scale,cfg,guidance_list,scheduler_choice):
    from comfy.model_management import cleanup_models
    from comfy.clip_vision import load
    tensor_a = phi2narry(image_1.copy())
    tensor_b = phi2narry(image_2.copy())
    in_img = torch.cat((tensor_a, tensor_b), dim=0)

    original_config_file = os.path.join(dir_path, 'config', 'sd_xl_base.yaml')
    if dif_repo:
        single_files = False
    elif not dif_repo and ckpt_path:
        single_files = True
    elif dif_repo and ckpt_path:
        single_files = False
    else:
        raise "no model"
    del pipe
    cleanup_models(keep_clone_weights_loaded=False)
    gc.collect()
    torch.cuda.empty_cache()
    if single_files:
        try:
            pipe = StableDiffusionXLPipeline.from_single_file(
                ckpt_path, original_config=original_config_file,
                torch_dtype=torch.float16)
        except:
            try:
                pipe = StableDiffusionXLPipeline.from_single_file(
                    ckpt_path, original_config_file=original_config_file,
                    torch_dtype=torch.float16)
            except:
                raise "load pipe error!,check you diffusers"
    else:
        pipe = StableDiffusionXLPipeline.from_pretrained(dif_repo, torch_dtype=torch.float16)

    if controlnet_model_path != "none":
        controlnet_model_path = folder_paths.get_full_path("controlnet", controlnet_model_path)
        controlnet = ControlNetModel.from_unet(pipe.unet)
        cn_state_dict = load_file(controlnet_model_path, device="cpu")
        controlnet.load_state_dict(cn_state_dict, strict=False)
        controlnet.to(torch.float16)
        try:
            pipe = StableDiffusionXLControlNetPipeline.from_single_file(ckpt_path,unet=pipe.unet,vae=pipe.vae, text_encoder=pipe.text_encoder,
                                                                        controlnet=controlnet,
                                                                        original_config=original_config_file,
                                                                        torch_dtype=torch.float16)
        except:
            try:
                pipe = StableDiffusionXLControlNetPipeline.from_single_file(ckpt_path,unet=pipe.unet,vae=pipe.vae, text_encoder=pipe.text_encoder,
                                                                            controlnet=controlnet,
                                                                            original_config_file=original_config_file,
                                                                            torch_dtype=torch.float16)
            except:
                raise "load pipe error!,check you diffusers"
    if lora:
        if lora in lora_lightning_list:
            pipe.load_lora_weights(lora_path)
            pipe.fuse_lora()
        else:
            pipe.load_lora_weights(lora_path, adapter_name=trigger_words)
            pipe.fuse_lora(adapter_names=[trigger_words, ], lora_scale=lora_scale)
    pipe.scheduler = scheduler_choice.from_config(pipe.scheduler.config)
    pipe.enable_xformers_memory_efficient_attention()
    pipe.enable_freeu(s1=0.6, s2=0.4, b1=1.1, b2=1.2)
    pipe.enable_vae_slicing()

    if device != "mps":
        pipe.enable_model_cpu_offload()
    torch.cuda.empty_cache()
    # 预加载 ms
    photomaker_local_path = os.path.join(photomaker_dir, "ms_adapter.bin")
    if not os.path.exists(photomaker_local_path):
        ms_path = hf_hub_download(
            repo_id="doge1516/MS-Diffusion",
            filename="ms_adapter.bin",
            repo_type="model",
            local_dir=photomaker_dir,
        )
    else:
        ms_path = photomaker_local_path
    ms_ckpt = get_instance_path(ms_path)
    image_processor = CLIPImageProcessor()
    image_encoder_type = "clip"
    cleanup_models(keep_clone_weights_loaded=False)
    image_encoder=load(clip_vision)
    use_repo = False
    config_path = os.path.join(dir_path, "config", "config.json")
    image_encoder_config = OmegaConf.load(config_path)
    image_encoder_projection_dim = image_encoder_config["vision_config"]["projection_dim"]
    num_tokens = 16
    image_proj_type = "resampler"
    latent_init_mode = "grounding"
    # latent_init_mode = "random"
    image_proj_model = Resampler(
        dim=1280,
        depth=4,
        dim_head=64,
        heads=20,
        num_queries=num_tokens,
        embedding_dim=image_encoder_config["vision_config"]["hidden_size"],
        output_dim=pipe.unet.config.cross_attention_dim,
        ff_mult=4,
        latent_init_mode=latent_init_mode,
        phrase_embeddings_dim=pipe.text_encoder.config.projection_dim,
    ).to(device,dtype=torch.float16)
    ms_model = MSAdapter(pipe.unet, image_proj_model, ckpt_path=ms_ckpt, device=device, num_tokens=num_tokens)
    ms_model.to(device,dtype=torch.float16)
    torch.cuda.empty_cache()
    input_images = [image_1, image_2]
    batch_size = 1
    guidance_list = guidance_list.strip().split(";")
    box_add = []  # 获取预设box
    for i in range(len(guidance_list)):
        box_add.append(get_float(guidance_list[i]))

    if mask_threshold == 0.:
        mask_threshold = None

    image_ouput = []

    # get role name
    role_a = char_origin[0].replace("]", "").replace("[", "")
    role_b = char_origin[1].replace("]", "").replace("[", "")
    # get n p prompt
    prompts_dual, negative_prompt = apply_style(
        style_name, prompts_dual, negative_prompt
    )

    # 添加Lora trigger
    add_trigger_words = "," + trigger_words + " " + "style" + ";"
    if lora:
        prompts_dual = remove_punctuation_from_strings(prompts_dual)
        if lora not in lora_lightning_list:  # 加速lora不需要trigger
            prompts_dual = [item + add_trigger_words for item in prompts_dual]


    prompts_dual = [item.replace(char_origin[0],char_describe[0]) for item in prompts_dual if char_origin[0] in item ]
    prompts_dual = [item.replace(char_origin[1], char_describe[1]) for item in prompts_dual if char_origin[1] in item]

    prompts_dual = [item.replace("[", " ", ).replace("]", " ", ) for item in prompts_dual]
    #print(prompts_dual)
    torch.cuda.empty_cache()

    phrases = [[role_a, role_b]]
    drop_grounding_tokens = [0]  # set to 1 if you want to drop the grounding tokens

    if mask_threshold:
        boxes = [box_add[:2]]
        print(f"Roles position on {boxes}")
        # boxes = [[[0., 0.25, 0.4, 0.75], [0.6, 0.25, 1., 0.75]]]  # man+women
    else:
        zero_list = [0 for _ in range(4)]
        boxes = [zero_list for _ in range(2)]
        boxes = [boxes]  # used if you want no layout guidance
        # print(boxes)

    if controlnet_model_path != "none":
        d1, _, _, _ = control_image.size()
        if d1 == 1:
            control_img_list = [control_image]
        else:
            control_img_list = torch.chunk(control_image, chunks=d1)
        j = 0
        for i, prompt in enumerate(prompts_dual):
            control_image = control_img_list[j]
            control_image = nomarl_upscale(control_image, width, height)
            j += 1
            # used to get the attention map, return zero if the phrase is not in the prompt
            phrase_idxes = [get_phrases_idx(pipe.tokenizer, phrases[0], prompt)]
            eot_idxes = [[get_eot_idx(pipe.tokenizer, prompt)] * len(phrases[0])]
            # print(phrase_idxes, eot_idxes)
            image_main = main_control(prompt, width, height, pipe, phrases, ms_model, input_images, batch_size,
                                      steps,
                                      seed, negative_prompt, role_scale, image_encoder, cfg,
                                      image_processor, boxes, mask_threshold, start_step, image_proj_type,
                                      image_encoder_type, drop_grounding_tokens, controlnet_scale,  control_image,
                                      phrase_idxes, eot_idxes,in_img,use_repo)

            image_ouput += image_main
            torch.cuda.empty_cache()
    else:
        for i, prompt in enumerate(prompts_dual):
            # used to get the attention map, return zero if the phrase is not in the prompt
            phrase_idxes = [get_phrases_idx(pipe.tokenizer, phrases[0], prompt)]
            eot_idxes = [[get_eot_idx(pipe.tokenizer, prompt)] * len(phrases[0])]
            # print(phrase_idxes, eot_idxes)
            image_main = main_normal(prompt, pipe, phrases, ms_model, input_images, batch_size, steps, seed,
                                          negative_prompt, role_scale, image_encoder, cfg, image_processor,
                                          boxes, mask_threshold, start_step, image_proj_type, image_encoder_type,
                                          drop_grounding_tokens, height, width, phrase_idxes, eot_idxes,in_img,use_repo)
            image_ouput += image_main
            torch.cuda.empty_cache()
    torch.cuda.empty_cache()
    return image_ouput


class Storydiffusion_Model_Loader:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "repo_id": ("STRING", {"default": ""}),
                "ckpt_name": (["none"]+folder_paths.get_filename_list("checkpoints"),),
                "vae_id":(["none"]+folder_paths.get_filename_list("vae"),),
                "character_weights": (character_weights,),
                "lora": (["none"] + folder_paths.get_filename_list("loras"),),
                "lora_scale": ("FLOAT", {"default": 0.8, "min": 0.1, "max": 1.0, "step": 0.1}),
                "trigger_words": ("STRING", {"default": "best quality"}),
                "scheduler": (scheduler_list,),
                "model_type": (["img2img", "txt2img"],),
                "id_number": ("INT", {"default": 2, "min": 1, "max": 2, "step": 1, "display": "number"}),
                "sa32_degree": (
                    "FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.1, "round": 0.01}),
                "sa64_degree": (
                    "FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.1, "round": 0.01}),
                "img_width": ("INT", {"default": 768, "min": 256, "max": 2048, "step": 32, "display": "number"}),
                "img_height": ("INT", {"default": 768, "min": 256, "max": 2048, "step": 32, "display": "number"}),
                "photomake_mode": (["v1", "v2"],),
                "easy_function": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ("STORY_DICT", )
    RETURN_NAMES = ("model", )
    FUNCTION = "story_model_loader"
    CATEGORY = "Storydiffusion"

    def instance_path(self, path, repo):
        if repo == "":
            if path == "none":
                repo = "none"
            else:
                model_path = get_local_path(file_path, path)
                repo = get_instance_path(model_path)
        return repo
    def story_model_loader(self, repo_id, ckpt_name, vae_id, character_weights, lora, lora_scale, trigger_words, scheduler,
                           model_type, id_number, sa32_degree, sa64_degree, img_width, img_height, photomake_mode, easy_function):

        scheduler_choice = get_scheduler(scheduler)
        if ckpt_name == "none":
            ckpt_path = None
        else:
            ckpt_path = folder_paths.get_full_path("checkpoints", ckpt_name)

        if character_weights != "none":
            character_weights_path = get_instance_path(os.path.join(base_pt, character_weights))
            weights_list = os.listdir(character_weights_path)
            if weights_list:
                char_files = character_weights_path
            else:
                char_files = ""
        else:
            char_files = ""

        easy_function = easy_function.strip().lower()

        if easy_function == "auraface":
            auraface = True
        else:
            auraface = False

        # 用来测试新功能的，目前输入 auraface 会开启phtomake V2的auraface人脸识别功能，会下载模型，注意联网。
        if easy_function == "nf4":
            NF4 = True
        else:
            NF4 = False

        # 下载 photomaker-v1 or v2 文件
        photomaker_path = os.path.join(photomaker_dir, f"photomaker-{photomake_mode}.bin")
        if photomake_mode == "v1":
            if not os.path.exists(photomaker_path):
                photomaker_path = hf_hub_download(
                    repo_id="TencentARC/PhotoMaker",
                    filename="photomaker-v1.bin",
                    local_dir=photomaker_dir,
                )
        else:
            if not os.path.exists(photomaker_path):
                photomaker_path = hf_hub_download(
                    repo_id="TencentARC/PhotoMaker-V2",
                    filename="photomaker-v2.bin",
                    local_dir=photomaker_dir,
                )

        # 加载 lora 的配置
        if lora != "none":
            lora_path = folder_paths.get_full_path("loras", lora)
            lora_path = get_instance_path(lora_path)
            if "/" in lora:
                lora = lora.split("/")[-1]
            if "\\" in lora:
                lora = lora.split("\\")[-1]
        else:
            lora_path = None
            lora = None

        # global
        global attn_procs
        global sa32, sa64, write, height, width
        global attn_count, total_count, id_length, total_length, cur_step

        sa32 = sa32_degree
        sa64 = sa64_degree
        attn_count = 0
        total_count = 0
        cur_step = 0
        id_length = id_number
        total_length = 5
        attn_procs = {}
        write = False
        height = img_height
        width = img_width

        # load model
        use_kolor = False
        use_flux = False
        if repo_id:
            if repo_id.rsplit("/")[-1] in "Kwai-Kolors/Kolors":
                use_kolor = True
            if repo_id.rsplit("/")[-1] in "black-forest-labs/FLUX.1-dev,black-forest-labs/FLUX.1-schnell":
                use_flux = True

        # 加载模型了
        if not repo_id and not ckpt_path:
            raise Exception("you need choice a ckpt_path model or input a repo_id")
        elif not repo_id and ckpt_path:  # load ckpt model
            pipe = load_models(ckpt_path, model_type=model_type, single_files=True, use_safetensors=True, photomake_mode=photomake_mode, photomaker_path=photomaker_path, lora=lora,
                               lora_path=lora_path,
                               trigger_words=trigger_words, lora_scale=lora_scale)
            set_attention_processor(pipe.unet, id_length, is_ipadapter=False)
        else:  # if repo or no ckpt, choice repo model
            # 各种类型的 model 加载
            if repo_id.rsplit("/")[-1] == "playground-v2.5-1024px-aesthetic":
                pipe = DiffusionPipeline.from_pretrained(
                    repo_id,
                    torch_dtype=torch.float16,
                )
                set_attention_processor(pipe.unet, id_length, is_ipadapter=False)
            elif repo_id.rsplit("/")[-1] == "sdxl-unstable-diffusers-y":
                pipe = StableDiffusionXLPipeline.from_pretrained(
                    repo_id, torch_dtype=torch.float16, use_safetensors=False
                )
                set_attention_processor(pipe.unet, id_length, is_ipadapter=False)
            elif use_kolor:  # Kolors model
                from .kolors.pipelines.pipeline_stable_diffusion_xl_chatglm_256 import \
                    StableDiffusionXLPipeline as StableDiffusionXLPipelineKolors
                from .kolors.models.modeling_chatglm import ChatGLMModel
                from .kolors.models.tokenization_chatglm import ChatGLMTokenizer
                from .kolors.models.unet_2d_condition import UNet2DConditionModel as UNet2DConditionModelkolor
                from .kolors.pipelines.pipeline_stable_diffusion_xl_chatglm_256_ipadapter import \
                    StableDiffusionXLPipeline as StableDiffusionXLPipelinekoloripadapter
                text_encoder = ChatGLMModel.from_pretrained(
                    f'{repo_id}/text_encoder', torch_dtype=torch.float16).half()
                vae = AutoencoderKL.from_pretrained(f"{repo_id}/vae", revision=None).half()
                tokenizer = ChatGLMTokenizer.from_pretrained(f'{repo_id}/text_encoder')
                scheduler = EulerDiscreteScheduler.from_pretrained(f"{repo_id}/scheduler")

                if model_type == "txt2img":
                    unet = UNet2DConditionModel.from_pretrained(f"{repo_id}/unet", revision=None,
                                                                use_safetensors=True).half()
                    pipe = StableDiffusionXLPipelineKolors(
                        vae=vae,
                        text_encoder=text_encoder,
                        tokenizer=tokenizer,
                        unet=unet,
                        scheduler=scheduler,
                        force_zeros_for_empty_prompt=False, )
                    set_attention_processor(pipe.unet, id_length, is_ipadapter=False)
                else:
                    image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                        f'{repo_id}/Kolors-IP-Adapter-Plus/image_encoder', ignore_mismatched_sizes=True).to(dtype=torch.float16)
                    ip_img_size = 336
                    clip_image_processor = CLIPImageProcessor(size=ip_img_size, crop_size=ip_img_size)
                    unet = UNet2DConditionModelkolor.from_pretrained(f"{repo_id}/unet", revision=None,).half()
                    pipe = StableDiffusionXLPipelinekoloripadapter(
                        vae=vae,
                        text_encoder=text_encoder,
                        tokenizer=tokenizer,
                        unet=unet,
                        scheduler=scheduler,
                        image_encoder=image_encoder,
                        feature_extractor=clip_image_processor,
                        force_zeros_for_empty_prompt=False
                    )
                    if hasattr(pipe.unet, 'encoder_hid_proj'):
                        pipe.unet.text_encoder_hid_proj = pipe.unet.encoder_hid_proj

                    pipe.load_ip_adapter(f'{repo_id}/Kolors-IP-Adapter-Plus', subfolder="",
                                         weight_name=["ip_adapter_plus_general.bin"])
                pipe.enable_model_cpu_offload()
            elif use_flux:  # FLUX model
                # pip install optimum-quanto
                # https://gist.github.com/AmericanPresidentJimmyCarter/873985638e1f3541ba8b00137e7dacd9
                timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                weight_transformer = os.path.join(folder_paths.models_dir, "checkpoints", f"transformer_{timestamp}.pt")
                dtype = torch.bfloat16
                if not ckpt_path:
                    from optimum.quanto import freeze, qfloat8, quantize
                    from diffusers.pipelines.flux.pipeline_flux import FluxPipeline
                    from diffusers import FlowMatchEulerDiscreteScheduler
                    from diffusers.models.transformers.transformer_flux import FluxTransformer2DModel
                    from transformers import CLIPTextModel, CLIPTokenizer, T5EncoderModel, T5TokenizerFast
                    revision = "refs/pr/1"
                    scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(repo_id, subfolder="scheduler",
                                                                                revision=revision)
                    text_encoder = CLIPTextModel.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=dtype)
                    tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=dtype)
                    text_encoder_2 = T5EncoderModel.from_pretrained(repo_id, subfolder="text_encoder_2",
                                                                    torch_dtype=dtype,
                                                                    revision=revision)
                    tokenizer_2 = T5TokenizerFast.from_pretrained(repo_id, subfolder="tokenizer_2",
                                                                  torch_dtype=dtype,
                                                                  revision=revision)
                    vae = AutoencoderKL.from_pretrained(repo_id, subfolder="vae", torch_dtype=dtype,
                                                        revision=revision)
                    transformer = FluxTransformer2DModel.from_pretrained(repo_id, subfolder="transformer",
                                                                         torch_dtype=dtype, revision=revision)
                    quantize(transformer, weights=qfloat8)
                    freeze(transformer)
                    print(f"saving fp8 pt on '{weight_transformer}'")
                    torch.save(transformer,
                               weight_transformer)  # https://pytorch.org/tutorials/beginner/saving_loading_models.html.
                    quantize(text_encoder_2, weights=qfloat8)
                    freeze(text_encoder_2)
                    pipe = FluxPipeline(
                        scheduler=scheduler,
                        text_encoder=text_encoder,
                        tokenizer=tokenizer,
                        text_encoder_2=None,
                        tokenizer_2=tokenizer_2,
                        vae=vae,
                        transformer=None,
                    )
                    pipe.text_encoder_2 = text_encoder_2
                    pipe.transformer = transformer

                else:  # flux diff unet ,diff 0.30
                    from diffusers import FluxTransformer2DModel, FluxPipeline
                    from transformers import T5EncoderModel, CLIPTextModel
                    from optimum.quanto import freeze, qfloat8, quantize
                    if NF4:
                        #https://github.com/huggingface/diffusers/issues/9165
                        from accelerate.utils import set_module_tensor_to_device, compute_module_sizes
                        from accelerate import init_empty_weights
                        from .utils.convert_nf4_flux import _replace_with_bnb_linear, create_quantized_param, \
                            check_quantized_param
                        import safetensors.torch
                        import gc
                        dtype = torch.bfloat16
                        is_torch_e4m3fn_available = hasattr(torch, "float8_e4m3fn")
                        original_state_dict = safetensors.torch.load_file(ckpt_path)
                        config_file = os.path.join(dir_path, "config.json")
                        with init_empty_weights():
                            config = FluxTransformer2DModel.load_config(config_file)
                            model = FluxTransformer2DModel.from_config(config).to(dtype)
                            expected_state_dict_keys = list(model.state_dict().keys())

                        _replace_with_bnb_linear(model, "nf4")

                        for param_name, param in original_state_dict.items():
                            if param_name not in expected_state_dict_keys:
                                continue

                            is_param_float8_e4m3fn = is_torch_e4m3fn_available and param.dtype == torch.float8_e4m3fn
                            if torch.is_floating_point(param) and not is_param_float8_e4m3fn:
                                param = param.to(dtype)

                            if not check_quantized_param(model, param_name):
                                set_module_tensor_to_device(model, param_name, device=0, value=param)
                            else:
                                create_quantized_param(
                                    model, param, param_name, target_device=0, state_dict=original_state_dict,
                                    pre_quantized=True
                                )

                        del original_state_dict
                        gc.collect()
                        pipe = FluxPipeline.from_pretrained(repo_id, transformer=model,torch_dtype=dtype)

                    else:
                        if os.path.splitext(ckpt_path)[-1] == ".pt":
                            transformer = torch.load(ckpt_path)
                            transformer.eval()
                        else:
                            #config_file = f"{repo_id}/transformer/config.json"
                            config_file = os.path.join(dir_path,"utils" "config.json")
                            transformer = FluxTransformer2DModel.from_single_file(ckpt_path, config=config_file,
                                                                                  torch_dtype=dtype)
                            quantize(transformer, weights=qfloat8)
                            freeze(transformer)
                        text_encoder_2 = T5EncoderModel.from_pretrained(repo_id, subfolder="text_encoder_2",
                                                                        torch_dtype=dtype)
                        quantize(text_encoder_2, weights=qfloat8)
                        freeze(text_encoder_2)

                        pipe = FluxPipeline.from_pretrained(repo_id, transformer=None, text_encoder_2=None,
                                                            torch_dtype=dtype)
                        pipe.transformer = transformer
                        pipe.text_encoder_2 = text_encoder_2

                pipe.enable_model_cpu_offload()
                if lora:
                    if "Hyper" not in lora_path:  # can't support Hyper now
                        if not NF4:
                            pipe.load_lora_weights(lora_path)
                            pipe.fuse_lora(lora_scale=0.125)  # lora_scale=0.125

            else:  # SD dif_repo
                pipe = load_models(repo_id, model_type=model_type, single_files=False, use_safetensors=True, photomake_mode=photomake_mode,
                                   photomaker_path=photomaker_path, lora=lora,
                                   lora_path=lora_path,
                                   trigger_words=trigger_words, lora_scale=lora_scale)
                set_attention_processor(pipe.unet, id_length, is_ipadapter=False)

        if vae_id != "none":
            if not use_flux:
                vae_id = folder_paths.get_full_path("vae", vae_id)
                pipe.vae = AutoencoderKL.from_single_file(vae_id, torch_dtype=torch.float16)

        load_chars = False
        if not use_kolor and not use_flux:
            pipe.scheduler = scheduler_choice.from_config(pipe.scheduler.config)
            pipe.enable_freeu(s1=0.6, s2=0.4, b1=1.1, b2=1.2)
            pipe.enable_vae_slicing()
            unet = pipe.unet
            load_chars = load_character_files_on_running(unet, character_files=char_files)
            if device != "mps":
                pipe.to("cuda")

        # if device != "mps":
        #     pipe.enable_model_cpu_offload()
        torch.cuda.empty_cache()
        model = {"pipe": pipe, "use_flux": use_flux, "use_kolor": use_kolor, "photomake_mode": photomake_mode, "trigger_words": trigger_words, "lora_scale": lora_scale,
                 "load_chars": load_chars, "repo_id": repo_id, "lora_path": lora_path, "ckpt_path": ckpt_path, "model_type": model_type, "lora": lora, "scheduler": scheduler, "auraface": auraface}
        return (model,)


class Storydiffusion_Sampler:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STORY_DICT",),
                "character_prompt": ("STRING", {"multiline": True,
                                                "default": "[Taylor] a woman img, wearing a white T-shirt, blue loose hair.\n"
                                                           "[Lecun] a man img,wearing a suit,black hair."}),
                "scene_prompts": ("STRING", {"multiline": True,
                                             "default": "[Taylor]wake up in the bed,medium shot;\n[Taylor]have breakfast by the window;\n[Lecun] drving on the road,medium shot;\n[Lecun]work in the company."}),
                "split_prompt": ("STRING", {"default": ""}),
                "negative_prompt": ("STRING", {"multiline": True,
                                               "default": "bad anatomy, bad hands, missing fingers, extra fingers, "
                                                          "three hands, three legs, bad arms, missing legs, "
                                                          "missing arms, poorly drawn face, bad face, fused face, "
                                                          "cloned face, three crus, fused feet, fused thigh, "
                                                          "extra crus, ugly fingers, horn,"
                                                          "amputation, disconnected limbs"}),
                "img_style": (
                    ["No_style", "Realistic", "Japanese_Anime", "Digital_Oil_Painting", "Pixar_Disney_Character",
                     "Photographic", "Comic_book",
                     "Line_art", "Black_and_White_Film_Noir", "Isometric_Rooms"],),
                "seed": ("INT", {"default": 0, "min": 0, "max": MAX_SEED}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "cfg": ("FLOAT", {"default": 7, "min": 0.1, "max": 10.0, "step": 0.1, "round": 0.01}),
                "ip_adapter_strength": ("FLOAT", {"default": 0.5, "min": 0.1, "max": 1.0, "step": 0.1, "round": 0.01}),
                "style_strength_ratio": ("INT", {"default": 20, "min": 10, "max": 50, "step": 1, "display": "number"}),
                "clip_vision": (clip_paths,),
                "role_scale": (
                    "FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.1, "round": 0.01}),
                "mask_threshold": (
                    "FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.1, "round": 0.01}),
                "start_step": ("INT", {"default": 5, "min": 1, "max": 1024}),
                "save_character": ("BOOLEAN", {"default": False},),
                "controlnet_model_path": (["none"] + folder_paths.get_filename_list("controlnet"),),
                "controlnet_scale": (
                    "FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.1, "round": 0.01}),
                "guidance_list": ("STRING", {"multiline": True, "default": "0., 0.25, 0.4, 0.75;0.6, 0.25, 1., 0.75"}),
            },
            "optional": {"image": ("IMAGE",),
                         "control_image": ("IMAGE",), },
            }

    RETURN_TYPES = ("IMAGE", "STRING",)
    RETURN_NAMES = ("image", "prompt_array",)
    FUNCTION = "story_sampler"
    CATEGORY = "Storydiffusion"

    def story_sampler(self, model, character_prompt, scene_prompts, split_prompt, negative_prompt, img_style, seed, steps,
                      cfg, ip_adapter_strength, style_strength_ratio, clip_vision,
                      role_scale, mask_threshold, start_step, save_character, controlnet_model_path, controlnet_scale, guidance_list, **kwargs):
        # get value from dict
        pipe = model.get("pipe")
        use_flux = model.get("use_flux")
        photomake_mode = model.get("photomake_mode")
        use_kolor = model.get("use_kolor")
        load_chars = model.get("load_chars")
        model_type = model.get("model_type")
        trigger_words = model.get("trigger_words")
        repo_id = model.get("repo_id")
        lora_path = model.get("lora_path")
        ckpt_path = model.get("ckpt_path")
        lora = model.get("lora")
        lora_scale = model.get("lora_scale")
        auraface = model.get("auraface")
        scheduler = model.get("scheduler")
        scheduler_choice = get_scheduler(scheduler)
        image = kwargs.get("image")

        # 格式化文字内容
        if split_prompt:
            scene_prompts.replace("\n", "").replace(split_prompt, ";\n").strip()
            character_prompt.replace("\n", "").replace(split_prompt, ";\n").strip()
        else:
            scene_prompts.strip()
            character_prompt.strip()
            if "\n" not in scene_prompts:
                scene_prompts.replace(";", ";\n").strip()
            if "\n" in character_prompt:
                if character_prompt.count("\n") > 1:
                    character_prompt.replace("\n", "").replace("[", "\n[").strip()
                    if character_prompt.count("\n") > 1:
                        character_prompt.replace("\n", "").replace("[", "\n[", 2).strip()  # 多行角色在这里强行转为双角色

        # 从角色列表获取角色方括号信息
        # at 2024.9.5 现在双角色同框的加载方式改成[A]...[B]...模式，原来的（A and B）模式已经摈弃！！！！
        char_origin = character_prompt.splitlines()
        char_describe = char_origin  # [A a men..., B a girl] 不对了？
        char_origin = [char.split("]")[0] + "]" for char in char_origin]

        # 判断是否有双角色prompt，如果有，获取双角色列表及对应的位置列表，
        prompts_origin = scene_prompts.splitlines()
        positions_dual = [index for index, prompt in enumerate(prompts_origin) if len(extract_content_from_brackets(prompt)) >= 2]  # 改成单句中双方括号方法，利于MS组句，[A]... [B]...[C]
        prompts_dual = [prompt for prompt in prompts_origin if len(extract_content_from_brackets(prompt)) >= 2]

        # positions_dual = [index for index, prompt in enumerate(prompts_origin) if has_parentheses(prompt)]
        # prompts_dual = [prompt for prompt in prompts_origin if has_parentheses(prompt)]

        print("多角色信息:", char_origin)

        if len(char_origin) == 2:
            positions_char_1 = [index for index, prompt in enumerate(prompts_origin) if char_origin[0] in prompt][0]  # 获取角色出现的索引列表，并获取首次出现的位置
            positions_char_2 = [index for index, prompt in enumerate(prompts_origin) if char_origin[1] in prompt][0]  # 获取角色出现的索引列表，并获取首次出现的位置

        if model_type == "img2img":
            d1, _, _, _ = image.size()
            if d1 == 1:
                image_load = [nomarl_upscale(image, width, height)]
            else:
                img_list = list(torch.chunk(image, chunks=d1))
                image_load = [nomarl_upscale(img, width, height) for img in img_list]

            gen = process_generation(pipe, image_load, model_type, steps, img_style, ip_adapter_strength,
                                     style_strength_ratio, cfg,
                                     seed, id_length,
                                     character_prompt,
                                     negative_prompt,
                                     scene_prompts,
                                     width,
                                     height,
                                     load_chars,
                                     lora,
                                     trigger_words, photomake_mode, use_kolor, use_flux, auraface)

        else:
            upload_images = None
            _Ip_Adapter_Strength = 0.5
            _style_strength_ratio = 20

            # 这个生成的是什么？
            gen = process_generation(pipe, upload_images, model_type, steps, img_style, _Ip_Adapter_Strength,
                                     _style_strength_ratio, cfg,
                                     seed, id_length,
                                     character_prompt,
                                     negative_prompt,
                                     scene_prompts,
                                     width,
                                     height,
                                     load_chars,
                                     lora,
                                     trigger_words, photomake_mode, use_kolor, use_flux, auraface)

        for value in gen:
            print("这是啥？", type(value))

        # 角色信息，图生图的时候使用
        image_pil_list = phi_list(value)

        image_pil_list_ms = image_pil_list.copy()
        if save_character:
            print("保存角色 saving character...")
            save_results(pipe.unet)

        if clip_vision == "none":
            clip_vision = None
        else:
            clip_vision = folder_paths.get_full_path("clip_vision", clip_vision)

        # 双角色
        if prompts_dual:
            if not clip_vision:
                raise Exception("need a clip_vison weight.")
            if use_flux or use_kolor:
                raise Exception("flux or kolor don't support MS diffsion.")

            print("开始启动双角色采样提示词 | start sampler dual prompt")

            control_image = None
            if controlnet_model_path != "none":
                control_image = kwargs["control_image"]

            # 角色信息 ...?
            image_a = image_pil_list_ms[positions_char_1]
            image_b = image_pil_list_ms[positions_char_2]
            image_dual = msdiffusion_main(pipe, image_a, image_b, prompts_dual, width, height, steps, seed,
                                          img_style, char_describe, char_origin, negative_prompt, clip_vision, model_type, lora, lora_path, lora_scale,
                                          trigger_words, ckpt_path, repo_id, role_scale,
                                          mask_threshold, start_step, controlnet_model_path, control_image, controlnet_scale, cfg, guidance_list, scheduler_choice)
            j = 0
            for i in positions_dual:  # 重新将双人场景插入原序列
                img = image_dual[j]
                image_pil_list.insert(int(i), img)
                j += 1
            image_list = narry_list(image_pil_list)
            torch.cuda.empty_cache()
        else:
            image_list = narry_list(image_pil_list)

        image = torch.from_numpy(np.fromiter(image_list, np.dtype((np.float32, (height, width, 3)))))
        torch.cuda.empty_cache()
        return (image, scene_prompts,)


class Comic_Type:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image": ("IMAGE",),
                             "scene_prompts": ("STRING", {"multiline": True, "forceInput": True, "default": ""}),
                             "fonts_list": (fonts_lists,),
                             "text_size": ("INT", {"default": 40, "min": 1, "max": 100}),
                             "comic_type": (["Four_Pannel", "Classic_Comic_Style"],),
                             "split_lines": ("STRING", {"default": "；"}),
                             }}

    RETURN_TYPES = ("IMAGE",)
    ETURN_NAMES = ("image",)
    FUNCTION = "comic_gen"
    CATEGORY = "Storydiffusion"

    def comic_gen(self, image, scene_prompts, fonts_list, text_size, comic_type, split_lines):
        result = [item for index, item in enumerate(image)]
        total_results = narry_list_pil(result)
        font_choice = os.path.join(dir_path, "fonts", fonts_list)
        captions = scene_prompts.splitlines()
        if len(captions) > 1:
            captions = [caption.replace("(", "").replace(")", "") if "(" or ")" in caption else caption
                        for caption in captions]  # del ()
            captions = [caption.replace("[NC]", "") for caption in captions]
            captions = [caption.replace("]", "").replace("[", "") for caption in captions]
            captions = [
                caption.split("#")[-1] if "#" in caption else caption
                for caption in captions
            ]
        else:
            prompt_array = scene_prompts.replace(split_lines, "\n")
            captions = prompt_array.splitlines()
        font = ImageFont.truetype(font_choice, text_size)
        images = (
                get_comic(total_results, comic_type, captions=captions, font=font)
                + total_results
        )
        images = phi2narry(images[0])
        return (images,)


class Pre_Translate_prompt:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"scene_prompts": ("STRING", {"forceInput": True, "default": ""}),
                             "keep_character_name": ("BOOLEAN", {"default": False},)
                             }}

    RETURN_TYPES = ("STRING",)
    ETURN_NAMES = ("prompt_array",)
    FUNCTION = "translate_prompt"
    CATEGORY = "Storydiffusion"

    def translate_prompt(self, scene_prompts, keep_character_name):
        captions = scene_prompts.splitlines()
        if not keep_character_name:
            captions = [caption.split(")", 1)[-1] if ")" in caption else caption
                        for caption in captions]  # del character
        else:
            captions = [caption.replace("(", "").replace(")", "") if "(" or ")" in caption else caption
                        for caption in captions]  # del ()
        captions = [caption.replace("[NC]", "") for caption in captions]
        if not keep_character_name:
            captions = [caption.split("]", 1)[-1] for caption in captions]
        else:
            captions = [caption.replace("]", "").replace("[", "") for caption in captions]
        captions = [
            caption.split("#")[-1] if "#" in caption else caption
            for caption in captions
        ]
        scene_prompts = ''.join(captions)
        return (scene_prompts,)


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "Storydiffusion_Model_Loader": Storydiffusion_Model_Loader,
    "Storydiffusion_Sampler": Storydiffusion_Sampler,
    "Pre_Translate_prompt": Pre_Translate_prompt,
    "Comic_Type": Comic_Type,
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "Storydiffusion_Model_Loader": "Storydiffusion_Model_Loader",
    "Storydiffusion_Sampler": "Storydiffusion_Sampler",
    "Pre_Translate_prompt": "Pre_Translate_prompt",
    "Comic_Type": "Comic_Type",
}
