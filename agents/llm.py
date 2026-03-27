"""LLM model handles.

Mirrors the cicero-chatbot pattern: a Groq primary model and an AWS Bedrock
fallback model. Construction is lazy (memoized) so the web app can boot and
serve the UI even before API keys are configured — only the parse path needs
a working model.
"""

from functools import lru_cache

from core.config import (
    BEDROCK_AWS_REGION,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    BEDROCK_MODEL_ID,
    GROQ_CHAT_MODEL,
    GROQ_TEMPERATURE,
    GROQ_API_KEY,
)


@lru_cache(maxsize=1)
def get_groq_model():
    from langchain_groq import ChatGroq
    return ChatGroq(api_key=GROQ_API_KEY, model=GROQ_CHAT_MODEL, temperature=GROQ_TEMPERATURE)


@lru_cache(maxsize=1)
def get_bedrock_model():
    from langchain_aws import ChatBedrockConverse
    return ChatBedrockConverse(
        model_id=BEDROCK_MODEL_ID,
        region_name=BEDROCK_AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
