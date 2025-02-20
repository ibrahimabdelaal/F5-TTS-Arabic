# ruff: noqa: E402
# Above allows ruff to ignore E402: module level import not at top of file

import re
import tempfile

import click
import gradio as gr
import numpy as np
import soundfile as sf
import torchaudio
from cached_path import cached_path
from transformers import AutoModelForCausalLM, AutoTokenizer
from num2words import num2words

try:
    import spaces

    USING_SPACES = True
except ImportError:
    USING_SPACES = False


def gpu_decorator(func):
    if USING_SPACES:
        return spaces.GPU(func)
    else:
        return func


from f5_tts.model import DiT, UNetT
from f5_tts.infer.utils_infer import (
    load_vocoder,
    load_model,
    preprocess_ref_audio_text,
    infer_process,
    remove_silence_for_generated_wav,
    save_spectrogram,
)

vocoder = load_vocoder()


# load models
F5TTS_model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
F5TTS_ema_model = load_model(
    DiT, F5TTS_model_cfg, str(cached_path("hf://IbrahimSalah/F5-TTS-Arabic/model_380000.pt"))
)

chat_model_state = None
chat_tokenizer_state = None


@gpu_decorator
def generate_response(messages, model, tokenizer):
    """Generate response using Qwen"""
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    model_inputs = tokenizer([text], return_tensors="pt").to(model.device)
    generated_ids = model.generate(
        **model_inputs,
        max_new_tokens=512,
        temperature=0.7,
        top_p=0.95,
    )

    generated_ids = [
        output_ids[len(input_ids) :] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]
    return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]

def convert_numbers_to_arabic_text(text):
    text_separated = re.sub(r'([A-Za-z])(\d)', r'\1 \2', text)
    text_separated = re.sub(r'(\d)([A-Za-z])', r'\1 \2', text_separated)
    
    def replace_number(match):
        number = match.group()
        return num2words(int(number), lang='ar')

    text_translated = re.sub(r'\b\d+\b', replace_number, text_separated)

    return text_translated

@gpu_decorator
def infer(
    ref_audio_orig, ref_text, gen_text, model, remove_silence, cross_fade_duration=0.15, speed=1, show_info=gr.Info
):
    ref_audio, ref_text = preprocess_ref_audio_text(ref_audio_orig, ref_text, show_info=show_info)

    ema_model = F5TTS_ema_model

    if not gen_text.startswith(" "):
        gen_text = " " + gen_text
    if not gen_text.endswith(". "):
        gen_text += ". "

    gen_text = gen_text.lower()
    gen_text = convert_numbers_to_arabic_text(gen_text)

    final_wave, final_sample_rate, combined_spectrogram = infer_process(
        ref_audio,
        ref_text,
        gen_text,
        ema_model,
        vocoder,
        cross_fade_duration=cross_fade_duration,
        speed=speed,
        show_info=show_info,
        progress=gr.Progress(),
    )

    # Remove silence
    if remove_silence:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            sf.write(f.name, final_wave, final_sample_rate)
            remove_silence_for_generated_wav(f.name)
            final_wave, _ = torchaudio.load(f.name)
        final_wave = final_wave.squeeze().cpu().numpy()

    # Save the spectrogram
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
        spectrogram_path = tmp_spectrogram.name
        save_spectrogram(combined_spectrogram, spectrogram_path)

    return (final_sample_rate, final_wave), spectrogram_path


with gr.Blocks() as app_credits:
    gr.Markdown("""
# الاعتمادات

* [mrfakename](https://github.com/fakerybakery) للنسخة التجريبية الأصلية على الإنترنت
* [RootingInLoad](https://github.com/RootingInLoad) لتوليد المقاطع الأولية واستكشاف تطبيق البودكاست
* [jpgallegoar](https://github.com/jpgallegoar) لتوليد أنماط الكلام المتعددة والدردشة الصوتية والتحسين باللغة العربية
""")


with gr.Blocks() as app_tts:
    gr.Markdown("# TTS ")
    ref_audio_input = gr.Audio(label="الصوت المرجعى", type="filepath")
    gen_text_input = gr.Textbox(label="الكلام المراد تجويله", lines=10)
    model_choice = gr.Radio(choices=["F5-TTS"], label="اختر نموذج TTS", value="F5-TTS")
    generate_btn = gr.Button("توليد", variant="primary")
    with gr.Accordion("الإعدادات المتقدمة", open=False):
        ref_text_input = gr.Textbox(
            label="النص المرجعى",
            info="اتركه فارغًا لتحويل الصوت تلقائيًا. إذا أدخلت نصًا، سيتم استبدال التحويل التلقائي.",
            lines=2,
        )
        remove_silence = gr.Checkbox(
            label="إزالة الصمت",
            info="يميل النموذج إلى إنتاج صمت، خاصةً في الصوت الأطول. يمكننا إزالة الصمت يدويًا إذا كان ذلك ضروريًا. يرجى ملاحظة أن هذا خاصية تجريبية وقد ينتج عنها نتائج غير متوقعة. سيزيد الوقت اللازم لإنشاء النتيجة أيضًا.",
            value=False,
        )
        speed_slider = gr.Slider(
            label="سرعة",
            minimum=0.3,
            maximum=2.0,
            value=1.0,
            step=0.1,
            info="ضبط سرعة الصوت.",
        )
        cross_fade_duration_slider = gr.Slider(
            label="مدة التحويل المتقاطعة (ثانية)",
            minimum=0.0,
            maximum=1.0,
            value=0.15,
            step=0.01,
            info="تحديد مدة التحويل المتقاطع بين المقاطع الصوتية.",
        )

    audio_output = gr.Audio(label="الصوت المولد")
    spectrogram_output = gr.Image(label="Espectrograma")
    print(ref_text_input)
    generate_btn.click(
        infer,
        inputs=[
            ref_audio_input,
            ref_text_input,
            gen_text_input,
            model_choice,
            remove_silence,
            cross_fade_duration_slider,
            speed_slider,
        ],
        outputs=[audio_output, spectrogram_output],
    )


def parse_speechtypes_text(gen_text):
    # Pattern to find {speechtype}
    pattern = r"\{(.*?)\}"

    # Split the text by the pattern
    tokens = re.split(pattern, gen_text)

    segments = []

    current_style = "Regular"

    for i in range(len(tokens)):
        if i % 2 == 0:
            # This is text
            text = tokens[i].strip()
            if text:
                segments.append({"style": current_style, "text": text})
        else:
            # This is style
            style = tokens[i].strip()
            current_style = style

    return segments


with gr.Blocks() as app_multistyle:
    # New section for multistyle generation
    gr.Markdown(
        """
    # إنشاء أنماط متعددة من الكلام

    يمكنك إنشاء أنماط متعددة من الكلام أو أصوات متعددة من الأشخاص. أدخل نصك بالشكل الموضح أدناه، وسيقوم النظام بإنشاء الكلام باستخدام النمط المناسب. إذا لم يتم تحديد، سيستخدم النموذج نمط الكلام العادي. سيستخدم النمط الحالي حتى يتم تحديد النمط التالي.
    """
    )

    with gr.Row():
        gr.Markdown(
            """
            **مثال على الإدخال:**                                                                      
            {عادي} مرحباً، أود طلب شطيرة من فضلك.                                                         
            {متفاجئ} ماذا تقصد بأنه لا يوجد خبز؟                                                                      
            {حزين} كنت أريد شطيرة حقاً...                                                              
            {غاضب} تعرف ماذا، لعنة عليك وعلى متجرك الصغير!                                                                       
            {هامس} سأعود إلى المنزل وأبكي الآن.                                                                           
            {صارخ} لماذا أنا؟!                                                                         
            """
        )

        gr.Markdown(
            """
            **مثال على الإدخال 2:**                                                                                
            {متحدث1_سعيد} مرحباً، أود طلب شطيرة من فضلك.                                                            
            {متحدث2_عادي} آسف، نفد الخبز لدينا.                                                                                
            {متحدث1_حزين} كنت أريد شطيرة حقاً...                                                                             
            {متحدث2_هامس} سأعطيك آخر واحدة كنت أخفيها.                                                                     
            """
        )

    gr.Markdown(
        "إرفع مقاطع صوتية مختلفة لكل نوع من الكلام. النوع الأول من الكلام إلزامي. يمكنك إضافة نوعين إضافيين من الكلام عن طريق الضغط على زر 'إضافة نوع من الكلام'."
    )

    # Regular speech type (mandatory)
    with gr.Row():
        with gr.Column():
            regular_name = gr.Textbox(value="Regular", label="اسم نوع الكلام")
            regular_insert = gr.Button("إدراج", variant="secondary")
        regular_audio = gr.Audio(label="Audio المرجعي العادي", type="filepath")
        regular_ref_text = gr.Textbox(label="Text المرجعي (عادي)", lines=2)

    # Additional speech types (up to 99 more)
    max_speech_types = 100
    speech_type_rows = []
    speech_type_names = [regular_name]
    speech_type_audios = []
    speech_type_ref_texts = []
    speech_type_delete_btns = []
    speech_type_insert_btns = []
    speech_type_insert_btns.append(regular_insert)

    for i in range(max_speech_types - 1):
        with gr.Row(visible=False) as row:
            with gr.Column():
                name_input = gr.Textbox(label="اسم نوع الكلام")
                delete_btn = gr.Button("إزالة", variant="secondary")
                insert_btn = gr.Button("إدراج", variant="secondary")
            audio_input = gr.Audio(label="Audio المرجعي", type="filepath")
            ref_text_input = gr.Textbox(label="Text المرجعي", lines=2)
        speech_type_rows.append(row)
        speech_type_names.append(name_input)
        speech_type_audios.append(audio_input)
        speech_type_ref_texts.append(ref_text_input)
        speech_type_delete_btns.append(delete_btn)
        speech_type_insert_btns.append(insert_btn)

    # Button to add speech type
    add_speech_type_btn = gr.Button("إضافة نوع من الكلام")

    # Keep track of current number of speech types
    speech_type_count = gr.State(value=0)

    # Function to add a speech type
    def add_speech_type_fn(speech_type_count):
        if speech_type_count < max_speech_types - 1:
            speech_type_count += 1
            # Prepare updates for the rows
            row_updates = []
            for i in range(max_speech_types - 1):
                if i < speech_type_count:
                    row_updates.append(gr.update(visible=True))
                else:
                    row_updates.append(gr.update())
        else:
            # Optionally, show a warning
            row_updates = [gr.update() for _ in range(max_speech_types - 1)]
        return [speech_type_count] + row_updates

    add_speech_type_btn.click(
        add_speech_type_fn, inputs=speech_type_count, outputs=[speech_type_count] + speech_type_rows
    )

    # Function to delete a speech type
    def make_delete_speech_type_fn(index):
        def delete_speech_type_fn(speech_type_count):
            # Prepare updates
            row_updates = []

            for i in range(max_speech_types - 1):
                if i == index:
                    row_updates.append(gr.update(visible=False))
                else:
                    row_updates.append(gr.update())

            speech_type_count = max(0, speech_type_count - 1)

            return [speech_type_count] + row_updates

        return delete_speech_type_fn

    # Update delete button clicks
    for i, delete_btn in enumerate(speech_type_delete_btns):
        delete_fn = make_delete_speech_type_fn(i)
        delete_btn.click(delete_fn, inputs=speech_type_count, outputs=[speech_type_count] + speech_type_rows)

    # Text input for the prompt
    gen_text_input_multistyle = gr.Textbox(
    label="نص التوليد",
    lines=10,
    placeholder="مرحبًا، أود طلب كوب من القهوة، من فضلك.",
)


    def make_insert_speech_type_fn(index):
        def insert_speech_type_fn(current_text, speech_type_name):
            current_text = current_text or ""
            speech_type_name = speech_type_name or "Ninguno"
            updated_text = current_text + f"{{{speech_type_name}}} "
            return gr.update(value=updated_text)

        return insert_speech_type_fn

    for i, insert_btn in enumerate(speech_type_insert_btns):
        insert_fn = make_insert_speech_type_fn(i)
        insert_btn.click(
            insert_fn,
            inputs=[gen_text_input_multistyle, speech_type_names[i]],
            outputs=gen_text_input_multistyle,
        )

    # Model choice
    model_choice_multistyle = gr.Radio(choices=["F5-TTS"], label="اختر نموذج TTS", value="F5-TTS")

    with gr.Accordion("الإعدادات المتقدمة", open=False):
        remove_silence_multistyle = gr.Checkbox(
            label="إزالة الصمت",
            value=False,
        )

    # زر التوليد
    generate_multistyle_btn = gr.Button("توليد الكلام", variant="primary")

    # إخراج الصوت
    audio_output_multistyle = gr.Audio(label="الصوت المُولّد")

    @gpu_decorator
    def generate_multistyle_speech(
        regular_audio,
        regular_ref_text,
        gen_text,
        *args,
    ):
        num_additional_speech_types = max_speech_types - 1
        speech_type_names_list = args[:num_additional_speech_types]
        speech_type_audios_list = args[num_additional_speech_types : 2 * num_additional_speech_types]
        speech_type_ref_texts_list = args[2 * num_additional_speech_types : 3 * num_additional_speech_types]
        model_choice = args[3 * num_additional_speech_types]
        remove_silence = args[3 * num_additional_speech_types + 1]

        # Collect the speech types and their audios into a dict
        speech_types = {"Regular": {"audio": regular_audio, "ref_text": regular_ref_text}}

        for name_input, audio_input, ref_text_input in zip(
            speech_type_names_list, speech_type_audios_list, speech_type_ref_texts_list
        ):
            if name_input and audio_input:
                speech_types[name_input] = {"audio": audio_input, "ref_text": ref_text_input}

        # Parse the gen_text into segments
        segments = parse_speechtypes_text(gen_text)

        # For each segment, generate speech
        generated_audio_segments = []
        current_style = "Regular"

        for segment in segments:
            style = segment["style"]
            text = segment["text"]

            if style in speech_types:
                current_style = style
            else:
                # If style not available, default to Regular
                current_style = "Regular"

            ref_audio = speech_types[current_style]["audio"]
            ref_text = speech_types[current_style].get("ref_text", "")

            # Generate speech for this segment
            audio, _ = infer(
                ref_audio, ref_text, text, model_choice, remove_silence, 0, show_info=print
            )  # show_info=print no pull to top when generating
            sr, audio_data = audio

            generated_audio_segments.append(audio_data)

        # Concatenate all audio segments
        if generated_audio_segments:
            final_audio_data = np.concatenate(generated_audio_segments)
            return (sr, final_audio_data)
        else:
            gr.Warning("لم يتم إنشاء أي صوت.")
            return None

    generate_multistyle_btn.click(
        generate_multistyle_speech,
        inputs=[
            regular_audio,
            regular_ref_text,
            gen_text_input_multistyle,
        ]
        + speech_type_names
        + speech_type_audios
        + speech_type_ref_texts
        + [
            model_choice_multistyle,
            remove_silence_multistyle,
        ],
        outputs=audio_output_multistyle,
    )

    # Validation function to disable Generate button if speech types are missing
    def validate_speech_types(gen_text, regular_name, *args):
        num_additional_speech_types = max_speech_types - 1
        speech_type_names_list = args[:num_additional_speech_types]

        # Collect the speech types names
        speech_types_available = set()
        if regular_name:
            speech_types_available.add(regular_name)
        for name_input in speech_type_names_list:
            if name_input:
                speech_types_available.add(name_input)

        # Parse the gen_text to get the speech types used
        segments = parse_speechtypes_text(gen_text)
        speech_types_in_text = set(segment["style"] for segment in segments)

        # Check if all speech types in text are available
        missing_speech_types = speech_types_in_text - speech_types_available

        if missing_speech_types:
            # Disable the generate button
            return gr.update(interactive=False)
        else:
            # Enable the generate button
            return gr.update(interactive=True)

    gen_text_input_multistyle.change(
        validate_speech_types,
        inputs=[gen_text_input_multistyle, regular_name] + speech_type_names,
        outputs=generate_multistyle_btn,
    )


with gr.Blocks() as app_chat:
    gr.Markdown(
        """
# دردشة صوتية  
تحدث مع الذكاء الاصطناعي باستخدام صوتك المرجعي!  
1. إرفع مقطع صوتي مرجعي، مع إمكانية إضافة النص المكتوب.  
2. حمل نموذج الدردشة.  
3. سجل رسالتك باستخدام الميكروفون.  
4. سيرد الذكاء الاصطناعي باستخدام الصوت المرجعي.  
"""
    )

    if not USING_SPACES:
        load_chat_model_btn = gr.Button("حمل نموذج الدردشة", variant="primary")

        chat_interface_container = gr.Column(visible=False)

        @gpu_decorator
        def load_chat_model():
            global chat_model_state, chat_tokenizer_state
            if chat_model_state is None:
                show_info = gr.Info
                show_info("جارٍ حمل نموذج الدردشة...")
                model_name = "Qwen/Qwen2.5-3B-Instruct"
                chat_model_state = AutoModelForCausalLM.from_pretrained(
                    model_name, torch_dtype="auto", device_map="auto"
                )
                chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)
                show_info("تم حمل نموذج الدردشة بنجاح.")

            return gr.update(visible=False), gr.update(visible=True)

        load_chat_model_btn.click(load_chat_model, outputs=[load_chat_model_btn, chat_interface_container])

    else:
        chat_interface_container = gr.Column()

        if chat_model_state is None:
            model_name = "Qwen/Qwen2.5-3B-Instruct"
            chat_model_state = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", device_map="auto")
            chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)

    with chat_interface_container:
        with gr.Row():
            with gr.Column():
                ref_audio_chat = gr.Audio(label="الصوت المرجعي", type="filepath")
            with gr.Column():
                with gr.Accordion("الإعدادات المتقدمة", open=False):
                    model_choice_chat = gr.Radio(
                        choices=["F5-TTS"],
                        label="نموذج تحويل النص إلى كلام",
                        value="F5-TTS",
                    )
                    remove_silence_chat = gr.Checkbox(
                        label="إزالة الصمت",
                        value=True,
                    )
                    ref_text_chat = gr.Textbox(
                        label="النص المرجعي",
                        info="اختياري: اتركه فارغًا ليتم تحويل الصوت إلى نص تلقائيًا",
                        lines=2,
                    )
                    system_prompt_chat = gr.Textbox(
                        label="توجيه النظام",
                        value="أنت لست مساعدًا ذكياً، أنت الشخص الذي يحدده المستخدم. يجب أن تحافظ على شخصيتك ولا تخرج عن الدور. اجعل إجاباتك قصيرة لأنها ستنطق بصوت عالٍ.",
                        lines=2,
                    )

        chatbot_interface = gr.Chatbot(label="الدردشة")

        with gr.Row():
            with gr.Column():
                audio_input_chat = gr.Microphone(
                    label="تحدث الآن",
                    type="filepath",
                )
                audio_output_chat = gr.Audio(autoplay=True)
            with gr.Column():
                text_input_chat = gr.Textbox(
                    label="أدخل رسالتك",
                    lines=1,
                )
                send_btn_chat = gr.Button("إرسال")
                clear_btn_chat = gr.Button("مسح الدردشة")

        conversation_state = gr.State(
            value=[
                {
                    "role": "system",
                    "content": "أنت لست مساعدًا ذكياً، أنت الشخص الذي يحدده المستخدم. يجب أن تحافظ على شخصيتك ولا تخرج عن الدور. اجعل إجاباتك قصيرة لأنها ستنطق بصوت عالٍ.",
                }
            ]
        )

        # Modify process_audio_input to use model and tokenizer from state
        @gpu_decorator
        def process_audio_input(audio_path, text, history, conv_state):
            """Handle audio or text input from user"""

            if not audio_path and not text.strip():
                return history, conv_state, ""

            if audio_path:
                text = preprocess_ref_audio_text(audio_path, text)[1]

            if not text.strip():
                return history, conv_state, ""

            conv_state.append({"role": "user", "content": text})
            history.append((text, None))

            response = generate_response(conv_state, chat_model_state, chat_tokenizer_state)

            conv_state.append({"role": "assistant", "content": response})
            history[-1] = (text, response)

            return history, conv_state, ""

        @gpu_decorator
        def generate_audio_response(history, ref_audio, ref_text, model, remove_silence):
            """Generate TTS audio for AI response"""
            if not history or not ref_audio:
                return None

            last_user_message, last_ai_response = history[-1]
            if not last_ai_response:
                return None

            audio_result, _ = infer(
                ref_audio,
                ref_text,
                last_ai_response,
                model,
                remove_silence,
                cross_fade_duration=0.15,
                speed=1.0,
                show_info=print,  # show_info=print no pull to top when generating
            )
            return audio_result

        def clear_conversation():
            """إعادة تعيين الدردشة"""
            return [], [
                {
                    "role": "system",
                    "content": "أنت لست مساعدًا ذكياً، أنت الشخص الذي يحدده المستخدم. يجب أن تحافظ على شخصيتك ولا تخرج عن الدور. اجعل إجاباتك قصيرة لأنها ستنطق بصوت عالٍ.",
                }
            ]

        def update_system_prompt(new_prompt):
            """Update the system prompt and reset the conversation"""
            new_conv_state = [{"role": "system", "content": new_prompt}]
            return [], new_conv_state

        # Handle audio input
        audio_input_chat.stop_recording(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            audio_input_chat,
        )

        # Handle text input
        text_input_chat.submit(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            text_input_chat,
        )

        # Handle send button
        send_btn_chat.click(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            text_input_chat,
        )

        # Handle clear button
        clear_btn_chat.click(
            clear_conversation,
            outputs=[chatbot_interface, conversation_state],
        )

        # Handle system prompt change and reset conversation
        system_prompt_chat.change(
            update_system_prompt,
            inputs=system_prompt_chat,
            outputs=[chatbot_interface, conversation_state],
        )



with gr.Blocks() as app:
    gr.Markdown(
        """
# 🎙️ Arabic-F5:تحويل النص إلى كلام عربى - نموذج مازال قيد التطوير! 🇸🇦🎶  

مرحبًا بك في **Arabic-F5**، النموذج المتطور لإنشاء كلام عربي طبيعي وسلس! 🏆  
يعتمد على أحدث التقنيات لجعل صوت الذكاء الاصطناعي أقرب ما يكون إلى الحقيقة.  

## 🚀 كيف تحصل على أفضل النتائج؟  
✅ استخدم ملف صوتي مرجعي بصيغة **WAV أو MP3**  
✅ تأكد أن مدة الصوت **بين 11 و 14 ثانية**  
✅ اترك **0.5 - 1 ثانية** من الصمت في البداية والنهاية للحصول على نتيجة مثالية  

🎯 **ملاحظة**: إذا لم تُدخل نصًا، سيتم تحويله تلقائيًا باستخدام Whisper. للحفاظ على جودة الأداء، اجعل المقاطع المرجعية قصيرة (<15 ثانية).  
�� **الأرقام تتحول تلقائيًا إلى كلمات باستخدام مكتبة num2words.**  

استمتع بتجربة الصوت الطبيعي الآن! 🎤✨  
"""
    )

    gr.TabbedInterface(
        [app_tts, app_multistyle, app_chat, app_credits],
        ["🗣️ تحويل النص إلى كلام", "🎭 أنماط صوتية متعددة", "💬 دردشة صوتية", "📜 الاعتمادات"],
    )

@click.command()
@click.option("--port", "-p", default=None, type=int, help="📡 المنفذ لتشغيل التطبيق")
@click.option("--host", "-H", default=None, help="🏠 المضيف لتشغيل التطبيق")
@click.option(
    "--share",
    "-s",
    default=False,
    is_flag=True,
    help="🔗 مشاركة التطبيق عبر رابط Gradio",
)
@click.option("--api", "-a", default=True, is_flag=True, help="🛠️ السماح بالوصول إلى API")
def main(port, host, share, api):
    global app
    print("🚀 جارٍ تشغيل Arabic-F5...")
    app.queue(api_open=api).launch(server_name=host, server_port=port, share=True, show_api=api)

if __name__ == "__main__":
    if not USING_SPACES:
        main()
    else:
        app.queue().launch()
