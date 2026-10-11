"""Model access: the plain SDK client (`client`) and the LangChain-backed
models the agent stack uses (`langchain_client`). Both write token usage
through `client._log_usage`, the single writer for `token_usage_log`."""
