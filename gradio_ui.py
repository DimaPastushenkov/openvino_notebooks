import gradio as gr
import socket
from stable_diffusion_engine import LatentConsistencyEngine
from diffusers import LCMScheduler
from superres import superres_load
import cv2
import numpy as np
import json
from pathlib import Path
from transformers import AutoConfig, AutoTokenizer
from optimum.intel.openvino import OVModelForCausalLM
from llm_config import SUPPORTED_LLM_MODELS
from pathlib import Path
import gradio as gr
import openvino as ov
import torch
from transformers import AutoTokenizer, TextIteratorStreamer
from PIL import Image
from pathlib import Path
from pipelines.nano_llava_utils import OVLlavaQwen2ForCausalLM
from threading import Thread


OCR = True
model_path = Path(f"dnd_models/square_lcm") 
f = open(r"locations.json")
locations_json = json.load(f)

def ready_ocr_model():
    ov_out_path = Path("dnd_models/ov_nanollava/INT4_compressed_weights")
    core = ov.Core()
    ocr_ov_model = OVLlavaQwen2ForCausalLM(core, ov_out_path, "GPU.0")
    ocr_tokenizer = AutoTokenizer.from_pretrained(Path("dnd_models/nanoLLaVA"), trust_remote_code=True)
    streamer = TextIteratorStreamer(ocr_tokenizer, skip_prompt=True, skip_special_tokens=True)
    return ocr_ov_model, ocr_tokenizer, streamer

def ready_llm_model():
    model_dir = r"C:\Users\riach\openvino_notebooks\notebooks\llm-chatbot\llama-3-8b-instruct\INT4_compressed_weights"
    print(f"Loading model from {model_dir}")
    ov_config = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1", "CACHE_DIR": "temp/"}
    model_configuration = SUPPORTED_LLM_MODELS["English"]["llama-3-8b-instruct"]
    model_name = model_configuration["model_id"]
    llm_tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
    llm_model = OVModelForCausalLM.from_pretrained(
        model_dir,
        device= "GPU.0",
        ov_config=ov_config,
        config=AutoConfig.from_pretrained(model_dir, trust_remote_code=True),
        trust_remote_code=True,
    )
    return llm_model, llm_tokenizer, model_configuration

print("Application Set Up - Please wait")
model_path_sr = Path(f"dnd_models/single-image-super-resolution-1033.xml") #realesrgan.xml")
engine = LatentConsistencyEngine(
model = model_path,
device = ["CPU", "GPU", "GPU"] 
) 
llm_model, llm_tokenizer, model_configuration = ready_llm_model()

if OCR is True:
    ocr_ov_model, ocr_tokenizer, streamer = ready_ocr_model()

print("Ready to launch")

def ocr_dice_roll(image, ocr_radio=False):
    print("OCR RADIO: ", ocr_radio)
    try:
        if ocr_radio == "yes":
            prompt = "What number did I just roll using the dice from the picture?"
        else:
            prompt= "Describe what you see in the image in 6 words ONLY."
        messages = [{"role": "user", "content": f"<image>\n{prompt}"}]
        text = ocr_tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        text_chunks = [ocr_tokenizer(chunk).input_ids for chunk in text.split("<image>")]
        input_ids = torch.tensor(text_chunks[0] + [-200] + text_chunks[1], dtype=torch.long).unsqueeze(0)
        #streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
        image_tensor = ocr_ov_model.process_images([image], ocr_ov_model.config)
        generation_kwargs = dict(
            input_ids=input_ids, images=image_tensor, streamer=streamer, max_new_tokens=128, temperature=0.01
        )
        thread = Thread(target=ocr_ov_model.generate, kwargs=generation_kwargs)
        thread.start()
        buffer = ""
        for new_text in streamer:
            buffer += new_text
            generated_text_without_prompt = buffer[:]
            #time.sleep(0.04)
        if ocr_radio == "yes":
            return generated_text_without_prompt, " "
        else: 
            return " ", generated_text_without_prompt
    except AttributeError:
        #No input image was passed
        pass

def add_theme(prompt, location):
    if location not in prompt:
        return f"{prompt} - {location}"
    
def adjust_theme(dice_roll_number, prompt=None):
    indexed_location = locations_json[str(dice_roll_number)]
    try:
        return indexed_location
    except:
        return "No theme"

def progress_callback(i, conn):
    tosend = bytes(str(i), 'utf-8')
    conn.sendall(tosend)

scheduler = LCMScheduler(
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear"
    )

from PIL import Image

def convert_result_to_image(result) -> np.ndarray:
    """
    Convert network result of floating point numbers to image with integer
    values from 0-255. Values outside this range are clipped to 0 and 255.

    :param result: a single superresolution network result in N,C,H,W shape
    """
    result = result.squeeze(0).transpose(1, 2, 0)
    result *= 255
    result[result < 0] = 0
    result[result > 255] = 255
    result = result.astype(np.uint8)
    return result

def run_sr(img):

    compiled_model, upsample_factor = superres_load(model_path_sr, "GPU")
  
    input_image_original = np.expand_dims(img.transpose(2, 0, 1), axis=0)
    bicubic_image = cv2.resize(
    src=img, dsize=(512*upsample_factor, 512*upsample_factor), interpolation=cv2.INTER_CUBIC)
    input_image_bicubic = np.expand_dims(bicubic_image.transpose(2, 0, 1), axis=0)

    original_image_key, bicubic_image_key = compiled_model.inputs
    output_key = compiled_model.output(0)

    result = compiled_model(
    {
        original_image_key.any_name: input_image_original,
        bicubic_image_key.any_name: input_image_bicubic,
    }
    )[output_key]

    result_image = convert_result_to_image(result)

    return result_image

def llama(random_num, text):
    #if first_run is True:
    tokenizer_kwargs = model_configuration.get("tokenizer_kwargs", {})
    test_string = f"""You are a Dungeons and Dragons prompt assistant who reads prompts and turns them into short prompts \
        for an image-generator. Rephrase the following sentence to be a descriptive prompt that is one short sentence only\
        and easy for a image generation model to understand, ending with proper punctuation. Add the theme to the prompt.): \
        ### Prompt: {text} \
        ### Theme: {locations_json[str(random_num)]} \
        ### Rephrased Prompt: """
    input_tokens = llm_tokenizer(test_string, return_tensors="pt", **tokenizer_kwargs)
    answer = llm_model.generate(**input_tokens, max_new_tokens=45)
    result = llm_tokenizer.batch_decode(answer, skip_special_tokens=True)[0]
    result = result.split('### Rephrased Prompt: ')[1]
    result = result.split('\n')[0]
    result = result.split('.')[0]
    #We can also ensure that the theme is infused, by manually adding the phrase to the end again
    #result = result + " (" + locations_json[str(random_num)] + ") "
    print(result)
    return result

def parse_ocr_output(text):
    try:
        return int(''.join(filter(str.isdigit, text)))
    except:
        #Detection did not work or image is empty
        return 1

def depth_map_parallax():
    #This function will load the OV Depth Anything model
    #and create a 3D parallax between the depth map and the input image
    #TBD how to create a GIF of the 3D parallax
    print("Depth Map WIP")

def generate_llm_prompt(text, dice_roll, _=gr.Progress(track_tqdm=True)):
   text = llama(dice_roll, text)
   return text  

def generate_from_text(dice_roll_num, orig_prompt, llm_prompt, seed, num_steps,guidance_input, _=gr.Progress(track_tqdm=True)):
   if llm_prompt == "": 
       text = orig_prompt + locations_json[str(dice_roll_num)]
   else:
       text = llm_prompt
   output = engine(
   prompt = text,
   num_inference_steps = num_steps,
   guidance_scale = guidance_input,
   scheduler = scheduler,
   lcm_origin_steps = 50,
   model = model_path,
   seed = seed
)
   img= cv2.cvtColor(np.array(output), cv2.COLOR_RGB2BGR)
   out = run_sr(img)
   return out  

def start(progress=gr.Progress()):
    
    HOST = "127.0.0.1" #"192.168.4.60"  # The server's hostname or IP address
    PORT = 65432  # The port used by the server
   
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall(b"start")
        data = s.recv(1024)
     

    print(f"Received {data!r}")
    progress(0, desc="Getting Ready - Please wait")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s_en:
        s_en.connect((HOST, 65433))
        data = s_en.recv(1024)
        if data.decode()=="speak":

            for i in progress.tqdm(range(100), desc="Listening", total=None, unit=""):
                data = s_en.recv(1024)
            
  
                if data.decode()=="continue": 
                     
                     print("updated progress continue")
                     continue
   
                if data.decode()=="stop_speak":
                    
                    break

def stop():
   
    HOST = "127.0.0.1"  #"192.168.4.60"  # The server's hostname or IP address
    PORT = 65432  # The port used by the server


    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        print("now stop")
        s.connect((HOST, PORT))
        s.sendall(b"stop")
        print("data sent")
        data = s.recv(1024)
        print("final data",data.decode())
   
     

    return data.decode()

def update_visibility(radio):  # Accept the event argument, even if not used
        value = radio  # Get the selected value from the radio button
        if value == "yes":
            return gr.Textbox(visible=bool(0)),  gr.Textbox(visible=bool(1))
        else:
            return gr.Textbox(visible=bool(0)), gr.Textbox(visible=bool(0))

css_code="""
.gradio-container { background:  url('file=assets/image_opt.jpg'); background-repeat: no-repeat; background-size: cover; background-position: center;}
h1 {
    text-align: center;
    font-size: 45px;
    display:block;
    font-family: fangsong 
}

#visible {background-color: rgba(255, 255, 255, 0.0); 
          border-color: rgba(255, 255, 255, 0.0);}
"""

_js="""
    () => {
        document.body.classList.toggle('dark') ;
        }
    """

"""theme = gr.themes.Default().set(button_primary_background_fill_dark="rgba(211, 211, 211, 0.1)",
                                button_primary_border_color_dark="rgba(211, 211, 211, 0.1)",
                                input_background_fill_dark="rgba(255, 255, 255, 0.1)",
                                block_background_fill_dark="rgba(211, 211, 211, 0.1)",
                                block_label_background_fill_dark="rgba(211, 211, 211, 0.0)",
                                border_color_primary_dark="rgba(211, 211, 211, 0.1)",
                                slider_color_dark="#f97316")"""
theme=gr.themes.Soft()

with gr.Blocks(css=css_code, js=_js, theme=theme) as demo:

    gr.Markdown(""" # 🏰 Bringing Adventure Gaming to Life 🧙 Using Real-time Generative AI on Your PC """)

    with gr.Row():
        with gr.Column(scale=1):
            radio = gr.Radio(["yes", "no"], label="Dice OCR")
            i = gr.Image(sources="webcam", label="Step 1: Roll Die / Dream", type="pil")
            ocr_output = gr.Textbox(label="Output of OCR Model", visible=False)
            #out = gr.Textbox(label="Number typed in", elem_id="visible")
            with gr.Row():
                dice_roll_input = gr.Textbox(lines=2, label="20-side Die Roll", container=True, placeholder="1", visible=True)
                dice_roll_theme = gr.Textbox(label="Theme", visible=True)
            with gr.Row():
                with gr.Row():
                    btn = gr.Button(value="Step 2: Start Rec", variant="primary")
                    stop_btn = gr.Button(value="Step 3: Stop Rec", variant="primary")
                text2 = gr.Textbox(label="Recording")
            #Prompts
            text_input = gr.Textbox(lines=3, label="Step 4.1: Your prompt",container=True,placeholder="Prompt")
            with gr.Row():
                add_theme_button = gr.Button(value="Step 4.2: Add theme to prompt", variant="primary")
                llm_button = gr.Button(value="Step 5: Refine Prompt with LLM", variant="primary")
            text_output = gr.Textbox(lines=3, label="LLM Prompt + Theme (or leave empty)", type="text", container=True, placeholder="LLM Prompt (Leave Empty to Discard)")
            #theme_options = gr.Dropdown(['None', 'Dark', 'Happy', 'Nostalgic'], label="Theme")
            image_btn = gr.Button(value="Step 6: Generate Image (Prompt + Theme)", variant="primary")
            #Parameters for LCM
            with gr.Accordion("Open for More Parameters!", open=False):
                seed_input = gr.Slider(0, 10000000, value=34, label="Seed")
                steps_input = gr.Slider(1, 50, value=5, step=1, label="Steps")
                guidance_input = gr.Slider(0, 15, value=2.0, label="Guidance")
        with gr.Column(scale=3):
            out = gr.Image(label="Result", type="pil", elem_id="visible")

    radio.change(update_visibility, radio, [ocr_output, dice_roll_input])        
    try:
        i.change(ocr_dice_roll, [i, radio], [ocr_output, dice_roll_theme])
    except ValueError:
        pass
    #the following lines of code only apply if we are looking at a dice roll
    ocr_output.change(parse_ocr_output, ocr_output, dice_roll_input)
    dice_roll_input.change(adjust_theme, dice_roll_input, dice_roll_theme)
    btn.click(start,outputs=text2)
    stop_btn.click(stop,outputs=text_input)
    add_theme_button.click(add_theme, [text_input, dice_roll_theme], text_input)
    llm_button.click(generate_llm_prompt, [text_input, dice_roll_input], text_output)
    #The LLM Generated Prompt can be left empty, and the image will be generated with the original prompt + theme
    image_btn.click(generate_from_text, [dice_roll_input, text_input, text_output, seed_input, steps_input, guidance_input], out)
    
    #with gr.Row():
        #with gr.Column(scale=1):
            #Image.fromarray(out).save("output.png")
            #filepath = Path("output.PNG").name
            #d = gr.DownloadButton("Download Smaller Image", value = filepath, visible=True)

try:
    demo.launch(share=True,debug=True,allowed_paths=['assets/image_opt.jpg',])
except Exception:
    demo.launch(share=True, debug=True,allowed_paths=['assets/image_opt.jpg',])