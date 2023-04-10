from tqdm import tqdm
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)

from chat.blip2 import Blip2

from easyinstruct.utils.api import set_openai_key, set_proxy
from easyinstruct.prompts import BasePrompt


set_openai_key("")
set_proxy("http://127.0.0.1:7890")


QUESTION_INSTRUCTION = (
    "I have an image. "
    "Ask me questions about the content of this image. "
    "Carefully asking me informative questions to maximize your information about this image content. "
    "Each time ask one question only without giving an answer. "
    "Avoid asking yes/no questions."
    'I\'ll put my answer beginning with "Answer:".'
)

SUB_QUESTION_INSTRUCTION = (
    "Next Question. Avoid asking yes/no questions. \n" "Question: "
)


SUMMARY_INSTRUCTION = (
    "Now summarize the information you get in a few sentences. "
    "Ignore the questions with answers no or not sure. "
    "Don't add information. Don't miss information. \n"
    "Summary: "
)


ANSWER_INSTRUCTION = "Answer given questions. If you are not sure about the answer, say you don't know honestly. Don't imagine any contents that are not in the image."


SUB_ANSWER_INSTRUCTION = "Answer: "  # template following blip2 huggingface demo


FIRST_QUESTION = "Describe this image in detail."


VALID_CHATGPT_MODELS = ["gpt-3.5-turbo"]
VALID_GPT3_MODELS = ["text-davinci-003", "text-davinci-002", "davinci"]


def get_instructions():
    instructions_dict = {
        "question": QUESTION_INSTRUCTION,
        "sub_question": SUB_QUESTION_INSTRUCTION,
        "summary": SUMMARY_INSTRUCTION,
        "answer": ANSWER_INSTRUCTION,
        "sub_answer": SUB_ANSWER_INSTRUCTION,
        "first_question": FIRST_QUESTION,
    }
    return instructions_dict


def get_chat_log(questions, answers, last_n=-1):
    n_addition_q = len(questions) - len(answers)
    assert (n_addition_q) in [0, 1]
    template = "Question: {} \nAnswer: {} \n"
    chat_log = ""
    if last_n > 0:
        answers = answers[-last_n:]
        questions = questions[-(last_n + n_addition_q) :]
    elif last_n == 0:
        answers = []
        questions = questions[-1:] if n_addition_q else []

    for i in range(len(answers)):
        chat_log = chat_log + template.format(questions[i], answers[i])
    if n_addition_q:
        chat_log = chat_log + "Question: {}".format(questions[-1])
    else:
        chat_log = chat_log[:-2]  # remove the last '/n'
    return chat_log


def prepare_gpt_prompt(task_prompt, questions, answers, sub_prompt):
    gpt_prompt = "\n".join([task_prompt, get_chat_log(questions, answers), sub_prompt])
    return gpt_prompt


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=2, max=120))
def call_gpt3(gpt3_prompt, max_tokens=40, model="text-davinci-003"):
    prompt_input = BasePrompt()
    prompt_input.build_prompt(gpt3_prompt)
    output = prompt_input.get_openai_result(engine=model, max_tokens=max_tokens)
    total_tokens = prompt_input.response["usage"]["total_tokens"]
    return output, total_tokens


def prepare_chatgpt_message(task_prompt, questions, answers, sub_prompt):
    messages = [{"role": "system", "content": task_prompt}]

    assert len(questions) == len(answers)
    for q, a in zip(questions, answers):
        messages.append({"role": "assistant", "content": "Question: {}".format(q)})
        messages.append({"role": "user", "content": "Answer: {}".format(a)})
    messages.append({"role": "system", "content": sub_prompt})

    return messages


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=2, max=120))
def call_chatgpt(chatgpt_messages, max_tokens=40, model="gpt-3.5-turbo"):
    prompt_input = BasePrompt()
    output = prompt_input.get_openai_result(
        engine=model,
        system_message=chatgpt_messages,
        temperature=0.6,
        max_tokens=max_tokens,
    )

    total_tokens = prompt_input.response["usage"]["total_tokens"]
    return output, total_tokens


class AskQuestions:
    def __init__(self, img, blip2, model, max_gpt_token=30, n_blip2_context=-1):
        self.img = img
        self.blip2 = blip2
        self.model = model
        self.max_gpt_token = max_gpt_token
        self.n_blip2_context = n_blip2_context

        self.questions = []
        self.answers = []
        self.total_tokens = 0

    def reset(self, img):
        self.img = img
        self.questions = []
        self.answers = []
        self.total_tokens = 0

    def ask_question(self):
        if len(self.questions) == 0:
            # first question is given by human to request a general discription
            question = FIRST_QUESTION
        else:
            if self.model in VALID_CHATGPT_MODELS:
                chatgpt_messages = prepare_chatgpt_message(
                    QUESTION_INSTRUCTION,
                    self.questions,
                    self.answers,
                    SUB_QUESTION_INSTRUCTION,
                )
                question, n_tokens = call_chatgpt(
                    chatgpt_messages, model=self.model, max_tokens=self.max_gpt_token
                )
            elif self.model in VALID_GPT3_MODELS:
                # prepare the context for GPT3
                gpt3_prompt = prepare_gpt_prompt(
                    QUESTION_INSTRUCTION,
                    self.questions,
                    self.answers,
                    SUB_QUESTION_INSTRUCTION,
                )

                question, n_tokens = call_gpt3(
                    gpt3_prompt, model=self.model, max_tokens=self.max_gpt_token
                )
            elif isinstance(self.model, Blip2):
                # prepare the context for other LLM
                gpt_prompt = prepare_gpt_prompt(
                    QUESTION_INSTRUCTION,
                    self.questions,
                    self.answers,
                    SUB_QUESTION_INSTRUCTION,
                )
                n_tokens = 0  # local model. no token cost on OpenAI API.
                question = self.model.call_llm(gpt_prompt)
            else:
                raise ValueError("{} is not a valid question model".format(self.model))

            self.total_tokens = self.total_tokens + n_tokens

        return question

    def question_trim(self, question):
        question = question.split("Question: ")[-1].replace("\n", " ").strip()
        if (
            "Answer:" in question
        ):  # Some models make up an answer after asking. remove it
            q, a = question.split("Answer:")[:2]
            if (
                len(q) == 0
            ):  # some not so clever models will put the question after 'Answer:'.
                question = a.strip()
            else:
                question = q.strip()
        return question

    def answer_question(self):
        # prepare the context for blip2
        blip2_prompt = "\n".join(
            [
                ANSWER_INSTRUCTION,
                get_chat_log(self.questions, self.answers, last_n=self.n_blip2_context),
                SUB_ANSWER_INSTRUCTION,
            ]
        )

        answer = self.blip2.ask(self.img, blip2_prompt)
        return answer

    def answer_trim(self, answer):
        answer = answer.split("Question:")[0].replace("\n", " ").strip()
        return answer

    def chatting(self, n_rounds, print_mode):
        if print_mode == "chat":
            print("--------Chat Starts----------")

        for i in tqdm(range(n_rounds), desc="Chat Rounds", disable=print_mode != "bar"):
            question = self.ask_question()
            # print('Raw: {}'.format(question))
            question = self.question_trim(question)
            self.questions.append(question)

            if print_mode == "chat":
                print("GPT-3: {}".format(question))
            elif print_mode == "gradio":
                gr_chatbot = gr_chatbot + [[question, None]]

            answer = self.answer_question()
            answer = self.answer_trim(answer)
            self.answers.append(answer)

            if print_mode == "chat":
                print("BLIP-2: {}".format(answer))
            elif print_mode == "gradio":
                self.gr_chatbot[-1][1] = answer

        if print_mode == "chat":
            print("--------Chat Ends----------")

        return self.questions, self.answers, self.total_tokens


def summarize_chat(questions, answers, model, max_gpt_token=100):
    if model in VALID_GPT3_MODELS:
        summary_prompt = prepare_gpt_prompt(
            QUESTION_INSTRUCTION, questions, answers, SUMMARY_INSTRUCTION
        )

        summary, n_tokens = call_gpt3(
            summary_prompt, model=model, max_tokens=max_gpt_token
        )
    elif model in VALID_CHATGPT_MODELS:
        summary_prompt = prepare_chatgpt_message(
            QUESTION_INSTRUCTION, questions, answers, SUMMARY_INSTRUCTION
        )
        summary, n_tokens = call_chatgpt(
            summary_prompt, model=model, max_tokens=max_gpt_token
        )
    elif isinstance(model, Blip2):
        summary_prompt = prepare_gpt_prompt(
            QUESTION_INSTRUCTION, questions, answers, SUMMARY_INSTRUCTION
        )
        n_tokens = 0  # local model. no token cost on OpenAI API.
        summary = model.call_llm(summary_prompt)
    else:
        raise ValueError("{} is not a valid question model".format(model))

    summary = summary.replace("\n", " ").strip()
    return summary, summary_prompt, n_tokens


def caption_image(
    blip2, image, model, n_rounds=10, n_blip2_context=-1, print_mode="no"
):
    if model == "gpt3":
        model = "text-davinci-003"
    elif model == "chatgpt":
        model = "gpt-3.5-turbo"

    results = {}
    chat = AskQuestions(image, blip2, n_blip2_context=n_blip2_context, model=model)

    questions, answers, n_token_chat = chat.chatting(n_rounds, print_mode=print_mode)

    summary, summary_prompt, n_token_sum = summarize_chat(
        questions, answers, model=model
    )
    results["ChatCaptioner"] = {
        "caption": summary,
        "chat": summary_prompt,
        "n_token": n_token_chat + n_token_sum,
    }
    results["BLIP2+OurPrompt"] = {"caption": answers[0]}

    # Default BLIP2 caption
    caption = blip2.caption(image)
    results["BLIP2"] = {"caption": caption}

    return results
