import os
import base64
import re
import json
import openai
import smtplib
import streamlit as st
import streamlit_authenticator as stauth
import toml

from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import AssistantEventHandler
from streamlit.components.v1 import html
from tools import TOOL_MAP
from typing_extensions import override


# Load the .toml file as a dictionary
with open(".streamlit/secrets.toml", "r") as f:
    service_account_info = toml.load(f)["service_account"]

st.set_page_config(
        page_title="Daniel DeBot",
        layout="wide",
        initial_sidebar_state="expanded"  # This line expands the sidebar by default
    )

load_dotenv()

def str_to_bool(str_input):
    if not isinstance(str_input, str):
        return False
    return str_input.lower() == "true"


# Load environment variables
openai_api_key = os.environ.get("OPENAI_API_KEY")
instructions = os.environ.get("RUN_INSTRUCTIONS", "")
enabled_file_upload_message = os.environ.get(
    "ENABLED_FILE_UPLOAD_MESSAGE", "Upload a file"
)
azure_openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
azure_openai_key = os.environ.get("AZURE_OPENAI_KEY")
authentication_required = str_to_bool(os.environ.get("AUTHENTICATION_REQUIRED", False))
email_address = os.environ.get("email_address")
storage_email_address = os.environ.get("storage_email_address")
email_password = os.environ.get("email_password")

# Load authentication configuration
if authentication_required:
    if "credentials" in st.secrets:
        authenticator = stauth.Authenticate(
            st.secrets["credentials"].to_dict(),
            st.secrets["cookie"]["name"],
            st.secrets["cookie"]["key"],
            st.secrets["cookie"]["expiry_days"],
        )
    else:
        authenticator = None  # No authentication should be performed

client = None
if azure_openai_endpoint and azure_openai_key:
    client = openai.AzureOpenAI(
        api_key=azure_openai_key,
        api_version="2024-05-01-preview",
        azure_endpoint=azure_openai_endpoint,
    )
else:
    client = openai.OpenAI(api_key=openai_api_key)


class EventHandler(AssistantEventHandler):
    @override
    def on_event(self, event):
        pass

    @override
    def on_text_created(self, text):
        st.session_state.current_message = ""
        with st.chat_message("Assistant"):
            st.session_state.current_markdown = st.empty()

    @override
    def on_text_delta(self, delta, snapshot):
        if snapshot.value:
            text_value = re.sub(
                r"\[(.*?)\]\s*\(\s*(.*?)\s*\)", "Download Link", snapshot.value
            )
            st.session_state.current_message = text_value
            st.session_state.current_markdown.markdown(
                st.session_state.current_message, True
            )

    @override
    def on_text_done(self, text):
        format_text = format_annotation(text)

        st.session_state.current_markdown.markdown(format_text, True)
        st.session_state.chat_log.append({"name": "assistant", "msg": format_text})

    @override
    def on_tool_call_created(self, tool_call):
        if tool_call.type == "code_interpreter":
            st.session_state.current_tool_input = ""
            with st.chat_message("Assistant"):
                st.session_state.current_tool_input_markdown = st.empty()

    @override
    def on_tool_call_delta(self, delta, snapshot):
        if 'current_tool_input_markdown' not in st.session_state:
            with st.chat_message("Assistant"):
                st.session_state.current_tool_input_markdown = st.empty()

        if delta.type == "code_interpreter":
            if delta.code_interpreter.input:
                st.session_state.current_tool_input += delta.code_interpreter.input
                input_code = f"### code interpreter\ninput:\n```python\n{st.session_state.current_tool_input}\n```"
                st.session_state.current_tool_input_markdown.markdown(input_code, True)

            if delta.code_interpreter.outputs:
                for output in delta.code_interpreter.outputs:
                    if output.type == "logs":
                        pass

    @override
    def on_tool_call_done(self, tool_call):
        st.session_state.tool_calls.append(tool_call)
        if tool_call.type == "code_interpreter":
            if tool_call.id in [x.id for x in st.session_state.tool_calls]:
                return
            input_code = f"### code interpreter\ninput:\n```python\n{tool_call.code_interpreter.input}\n```"
            st.session_state.current_tool_input_markdown.markdown(input_code, True)
            st.session_state.chat_log.append({"name": "assistant", "msg": input_code})
            st.session_state.current_tool_input_markdown = None
            for output in tool_call.code_interpreter.outputs:
                if output.type == "logs":
                    output = f"### code interpreter\noutput:\n```\n{output.logs}\n```"
                    with st.chat_message("Assistant"):
                        st.markdown(output, True)
                        st.session_state.chat_log.append(
                            {"name": "assistant", "msg": output}
                        )
        elif (
            tool_call.type == "function"
            and self.current_run.status == "requires_action"
        ):
            with st.chat_message("Assistant"):
                msg = f"### Function Calling: {tool_call.function.name}"
                st.markdown(msg, True)
                st.session_state.chat_log.append({"name": "assistant", "msg": msg})
            tool_calls = self.current_run.required_action.submit_tool_outputs.tool_calls
            tool_outputs = []
            for submit_tool_call in tool_calls:
                tool_function_name = submit_tool_call.function.name
                tool_function_arguments = json.loads(
                    submit_tool_call.function.arguments
                )
                tool_function_output = TOOL_MAP[tool_function_name](
                    **tool_function_arguments
                )
                tool_outputs.append(
                    {
                        "tool_call_id": submit_tool_call.id,
                        "output": tool_function_output,
                    }
                )

            with client.beta.threads.runs.submit_tool_outputs_stream(
                thread_id=st.session_state.thread.id,
                run_id=self.current_run.id,
                tool_outputs=tool_outputs,
                event_handler=EventHandler(),
            ) as stream:
                stream.until_done()


def create_thread(content, file):
    return client.beta.threads.create()


def create_message(thread, content, file):
    attachments = []
    if file is not None:
        attachments.append(
            {"file_id": file.id, "tools": [{"type": "code_interpreter"}, {"type": "file_search"}]}
        )
    client.beta.threads.messages.create(
        thread_id=thread.id, role="user", content=content, attachments=attachments
    )


def create_file_link(file_name, file_id):
    content = client.files.content(file_id)
    content_type = content.response.headers["content-type"]
    b64 = base64.b64encode(content.text.encode(content.encoding)).decode()
    link_tag = f'<a href="data:{content_type};base64,{b64}" download="{file_name}">Download Link</a>'
    return link_tag


def format_annotation(text):
    citations = []
    text_value = text.value
    # for index, annotation in enumerate(text.annotations):
    #     text_value = text_value.replace(annotation.text, f" [{index}]")

    #     if file_citation := getattr(annotation, "file_citation", None):
    #         cited_file = client.files.retrieve(file_citation.file_id)
    #         citations.append(
    #             f"[{index}] {file_citation.quote} from {cited_file.filename}"
    #         )
    #     elif file_path := getattr(annotation, "file_path", None):
    #         link_tag = create_file_link(
    #             annotation.text.split("/")[-1],
    #             file_path.file_id,
    #         )
    #         text_value = re.sub(r"\[(.*?)\]\s*\(\s*(.*?)\s*\)", link_tag, text_value)

    text_value = re.sub('【.*?†source】', '', text_value)
    # text_value += "\n\n" + "\n".join(citations)
    return text_value


def run_stream(user_input, file, selected_assistant_id):
    if "thread" not in st.session_state:
        st.session_state.thread = create_thread(user_input, file)
    create_message(st.session_state.thread, user_input, file)
    with client.beta.threads.runs.stream(
        thread_id=st.session_state.thread.id,
        assistant_id=selected_assistant_id,
        event_handler=EventHandler(),
    ) as stream:
        stream.until_done()


def handle_uploaded_file(uploaded_file):
    file = client.files.create(file=uploaded_file, purpose="assistants")
    return file


def render_chat():
    for chat in st.session_state.chat_log:
        with st.chat_message(chat["name"]):
            st.markdown(chat["msg"], True)


if "tool_call" not in st.session_state:
    st.session_state.tool_calls = []

if "chat_log" not in st.session_state:
    st.session_state.chat_log = []

if "in_progress" not in st.session_state:
    st.session_state.in_progress = False


def disable_form():
    st.session_state.in_progress = True


def login():
    if st.session_state["authentication_status"] is False:
        st.error("Username/password is incorrect")
    elif st.session_state["authentication_status"] is None:
        st.warning("Please enter your username and password")


def reset_chat():
    st.session_state.chat_log = []
    st.session_state.in_progress = False


def load_chat_screen(assistant_id, assistant_title):

    subtitle_warning = f"{assistant_title} is actively in development. Outputs should be manually reviewed."

    if False:
        uploaded_file = st.sidebar.file_uploader(
            enabled_file_upload_message,
            type=[
                "txt",
                "pdf",
                "csv",
                "json",
                "geojson",
                "xlsx",
                "xls",
            ],
            disabled=st.session_state.in_progress,
        )
    else:
        uploaded_file = None

    st.title(assistant_title if assistant_title else "")
    st.markdown(f"<p style='font-size:16px; color:gray;'>{subtitle_warning}</p>", unsafe_allow_html=True)

    user_msg = st.chat_input(
        "Message", on_submit=disable_form, disabled=st.session_state.in_progress
    )
    if user_msg:
        render_chat()
        with st.chat_message("user"):
            st.markdown(user_msg, True)
        st.session_state.chat_log.append({"name": "user", "msg": user_msg})

        file = None
        if uploaded_file is not None:
            file = handle_uploaded_file(uploaded_file)
        run_stream(user_msg, file, assistant_id)
        st.session_state.in_progress = False
        st.session_state.tool_call = None
        st.rerun()

    render_chat()


def main():

    # JavaScript for auto-scrolling
    scroll_script = """
        <script>
            var chatBox = parent.document.getElementsByClassName('main')[0];
            chatBox.scrollTop = chatBox.scrollHeight;
        </script>
    """
    html(scroll_script)

    # Check if multi-agent settings are defined
    multi_agents = os.environ.get("OPENAI_ASSISTANTS", None)
    single_agent_id = os.environ.get("ASSISTANT_ID", None)
    single_agent_title = os.environ.get("ASSISTANT_TITLE", "Assistants API UI")

    if (
        authentication_required
        and "credentials" in st.secrets
        and authenticator is not None
    ):
        authenticator.login()
        if not st.session_state["authentication_status"]:
            login()
            return
        else:
            authenticator.logout(location="sidebar")

    if multi_agents:
        assistants_json = json.loads(multi_agents)
        assistants_object = {f'{obj["title"]}': obj for obj in assistants_json}
        selected_assistant = st.sidebar.selectbox(
            "Select an assistant profile?",
            list(assistants_object.keys()),
            index=None,
            placeholder="Select an assistant profile...",
            on_change=reset_chat,  # Call the reset function on change
        )
        if selected_assistant:
            load_chat_screen(
                assistants_object[selected_assistant]["id"],
                assistants_object[selected_assistant]["title"],
            )
    elif single_agent_id:
        load_chat_screen(single_agent_id, single_agent_title)
    else:
        st.error("No assistant configurations defined in environment variables.")


def send_email_log(log_content):
    sender_email = email_address
    receiver_email = storage_email_address
    password = os.getenv("email_password")

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = "Rusty Data Manual User Log"
    msg.attach(MIMEText(log_content, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
            st.sidebar.success("Email sent successfully!")
    except Exception as e:
        st.sidebar.error(f"Failed to send email: {e}")


def update_logging_google_doc(conversation_log):
    # Create credentials from the service account info
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=["https://www.googleapis.com/auth/documents"]
    )

    # Initialize the Google Docs API client
    service = build("docs", "v1", credentials=credentials)

    # Example of appending text to an existing document
    DOCUMENT_ID = "1kei8AwNcHjUPimASzCn26AWyiOsMb42ZnJUd4NXhi4w"  # Replace with your actual document ID

    # Retrieve the document to find the last index
    # Call the function to insert text
    requests = insert_text(conversation_log=conversation_log)

    service.documents().batchUpdate(
        documentId=DOCUMENT_ID, body={"requests": requests}
    ).execute()

    print("Content appended successfully!")


# Define function to add content to Google Docs
def insert_text(conversation_log):
    doc_structure = [{"type": "header", "text": "START OF LOG"}]
    for content in conversation_log:
        print(type(conversation_log))
        print(conversation_log)
        print("zz")
        print(content)
        current_message_sender = content["name"]
        current_message = content["msg"]
        doc_structure.append({"type": "paragraph", "text": current_message_sender})
        doc_structure.append({"type": "paragraph", "text": current_message})

    doc_structure.append({"type": "header", "text": "END OF LOG\n\n\n"})
    requests = []

    for item in reversed(doc_structure):
        if item["text"] in ("user", "assistant"):
            # Insert bold header text
            requests.append(
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": item["text"] + ":\n",
                    }
                }
            )
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": 1 + len(item["text"]),
                        },
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                }
            )
        elif item["type"] in ["paragraph", "header"]:
            # Regular paragraph text
            requests.append(
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": item["text"] + "\n\n",
                    }
                }
            )
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": 1 + len(item["text"]),
                        },
                        "textStyle": {"bold": False},
                        "fields": "bold",
                    }
                }
            )

    return requests


# Sidebar button to send conversation log
st.sidebar.write("If responses are not accurate, please use the button below to log your recent chat history.")
user_comment = st.sidebar.text_input("Optional Comments", placeholder="Enter your comments here...")
if st.sidebar.button("Log Chat History"):
    # Assume `conversation_log` is a variable that stores the current conversation
    conversation_log = st.session_state.chat_log

    update_logging_google_doc(conversation_log)

    if user_comment:
        comment_append_conversation_log = f"{conversation_log}\n\nUser comments:\n{user_comment}"
    else:
        comment_append_conversation_log = conversation_log
    send_email_log(comment_append_conversation_log)



if __name__ == "__main__":
    main()
