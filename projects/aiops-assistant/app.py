"""
AIOps Assistant — Streamlit Chat UI
Runs the Kira agent loop (agent.py): Claude on Bedrock calling the aiops Lambdas as tools.

Setup:
    1. pip install -r requirements.txt
    2. cp .env.example .env
    3. Fill in your values in .env
    4. streamlit run app.py
"""

import streamlit as st
import boto3
import uuid
import os
from dotenv import load_dotenv

# Load environment variables from .env file (before agent reads AWS_REGION / BEDROCK_MODEL_ID)
load_dotenv()

from agent import KiraAgent, MODEL_ID  # noqa: E402

# --- Config from environment ---
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


# --- Page Config ---
st.set_page_config(
    page_title="Kira — AIOps Assistant",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- Custom CSS ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=DM+Sans:wght@400;500;700&display=swap');

    .stApp {
        background-color: #0a0e14;
        color: #c5c8c6;
    }

    .main-header {
        padding: 1.5rem 0 1rem 0;
        border-bottom: 1px solid #1a1f2e;
        margin-bottom: 1.5rem;
    }
    .main-header h1 {
        font-family: 'JetBrains Mono', monospace;
        color: #22d3ee;
        font-size: 1.6rem;
        font-weight: 700;
        margin: 0;
        letter-spacing: -0.5px;
    }
    .main-header p {
        font-family: 'DM Sans', sans-serif;
        color: #5a6270;
        font-size: 0.85rem;
        margin: 0.3rem 0 0 0;
    }

    .status-bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.5rem 1rem;
        background: #0d1117;
        border: 1px solid #1a1f2e;
        border-radius: 6px;
        margin-bottom: 1rem;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.75rem;
    }
    .status-dot {
        width: 8px;
        height: 8px;
        background: #22d3ee;
        border-radius: 50%;
        box-shadow: 0 0 6px #22d3ee;
        animation: pulse 2s infinite;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }

    .status-dot-error {
        width: 8px;
        height: 8px;
        background: #ef4444;
        border-radius: 50%;
        box-shadow: 0 0 6px #ef4444;
    }

    .stChatMessage {
        background: #0d1117 !important;
        border: 1px solid #1a1f2e !important;
        border-radius: 8px !important;
        font-family: 'DM Sans', sans-serif !important;
    }

    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
        background: #111820 !important;
        border-left: 3px solid #22d3ee !important;
    }

    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
        background: #0d1117 !important;
        border-left: 3px solid #f97316 !important;
    }

    .stChatInput textarea {
        font-family: 'DM Sans', sans-serif !important;
        background: #0d1117 !important;
        color: #c5c8c6 !important;
    }

    [data-testid="stSidebar"] {
        background: #0d1117;
        border-right: 1px solid #1a1f2e;
    }

    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #0a0e14; }
    ::-webkit-scrollbar-thumb { background: #1a1f2e; border-radius: 3px; }

    .stButton > button {
        background: #111820 !important;
        border: 1px solid #1a1f2e !important;
        color: #8b95a5 !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.75rem !important;
        padding: 0.4rem 0.8rem !important;
        border-radius: 4px !important;
        transition: all 0.2s !important;
    }
    .stButton > button:hover {
        border-color: #22d3ee !important;
        color: #22d3ee !important;
        background: #0d1117 !important;
    }

    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# --- Initialize Session State ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "converse_history" not in st.session_state:
    st.session_state.converse_history = []  # Converse API messages incl. tool calls


# --- Kira Agent ---
# Access keys optional: boto3 uses ~/.aws/credentials, SSO, env, or IAM role if unset.
@st.cache_resource
def get_agent():
    kwargs = {"region_name": AWS_REGION}
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
        if AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = AWS_SESSION_TOKEN
    return KiraAgent(boto3.Session(**kwargs))


def invoke_agent(prompt: str, status) -> str:
    """Run one turn of the Kira agent loop, logging each tool call to `status`."""
    history = st.session_state.converse_history
    turn_start = len(history)
    history.append({"role": "user", "content": [{"text": prompt}]})

    def on_tool_call(name, tool_input):
        args = ", ".join(f"{k}={v}" for k, v in tool_input.items())
        status.write(f"🔧 `{name}({args})`")

    try:
        return get_agent().chat(history, on_tool_call=on_tool_call)
    except Exception as e:
        del history[turn_start:]  # drop the partial turn so the next one starts clean
        return f"⚠️ Error: {str(e)}"


# --- Header ---
st.markdown("""
<div class="main-header">
    <h1>⚡ KIRA</h1>
    <p>AIOps Assistant — Root Cause Analysis Engine</p>
</div>
""", unsafe_allow_html=True)


# --- Status Bar ---
st.markdown(f"""
<div class="status-bar">
    <div class="status-dot"></div>
    <span style="color: #22d3ee;">ONLINE</span>
    <span style="color: #2a3040;">|</span>
    <span style="color: #5a6270;">Session: {st.session_state.session_id[:8]}</span>
    <span style="color: #2a3040;">|</span>
    <span style="color: #5a6270;">Region: {AWS_REGION}</span>
    <span style="color: #2a3040;">|</span>
    <span style="color: #5a6270;">Model: {MODEL_ID}</span>
</div>
""", unsafe_allow_html=True)


# --- Quick Actions ---
col1, col2, col3, col4 = st.columns(4)
with col1:
    if st.button("🔴 Check 503 errors"):
        st.session_state.quick_action = "Why are we seeing 503 errors in the last hour?"
with col2:
    if st.button("📊 CPU & Memory"):
        st.session_state.quick_action = "Check CPU and memory utilization across all services"
with col3:
    if st.button("🗄️ Database health"):
        st.session_state.quick_action = "Is the database healthy? Check connections and latency"
with col4:
    if st.button("🔍 Recent errors"):
        st.session_state.quick_action = "What are the most frequent errors in the last hour?"

st.markdown("<div style='height: 0.5rem'></div>", unsafe_allow_html=True)


# --- Chat History ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


# --- Handle Quick Actions ---
quick_action = st.session_state.pop("quick_action", None)


# --- Chat Input ---
user_input = st.chat_input("Describe the issue... e.g. 'Why is the API slow?'")

prompt = quick_action or user_input

if prompt:
    # Show user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Get agent response
    with st.chat_message("assistant"):
        with st.status("🔍 Kira is investigating...", expanded=True) as status:
            response = invoke_agent(prompt, status)
            status.update(label="✅ Investigation complete", state="complete", expanded=False)
        st.markdown(response)

    st.session_state.messages.append({"role": "assistant", "content": response})


# --- Sidebar ---
with st.sidebar:
    st.markdown("""
    <div style="font-family: 'JetBrains Mono', monospace; padding: 1rem 0;">
        <h3 style="color: #22d3ee; font-size: 1rem;">⚡ KIRA</h3>
        <p style="color: #5a6270; font-size: 0.8rem;">AIOps Assistant v1.0</p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("**Tools Available:**")
    st.markdown("- 📋 `fetch_logs` — CloudWatch Logs")
    st.markdown("- 📊 `fetch_metrics` — Prometheus pod metrics")
    st.markdown("- 🏥 `fetch_service_health` — EKS cluster & pods")

    st.markdown("---")
    st.markdown("**Sample Questions:**")
    st.markdown("""
    - Why are we seeing 503 errors?
    - Is CPU usage high?
    - Check database connections
    - Are all services healthy?
    - What errors happened in the last 2 hours?
    - Is there a memory leak?
    """)

    st.markdown("---")
    if st.button("🔄 New Session"):
        st.session_state.messages = []
        st.session_state.converse_history = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()