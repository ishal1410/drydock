"""python -m llm.smoke: ONE real call; prints model id, token usage and latency. Reads .env (values never printed)."""
import sys

from llm.client import ModelError, chat, load_env


def main(transport=None) -> int:
    try:
        # 256, not 16: a reasoning model can spend tokens before "OK"; a cut-off reply is ModelTruncated (H5)
        r = chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=256,
                 transport=transport)
    except ModelError as e:
        print(f"{type(e).__name__}: {e.public} ({e.detail})")  # terminal only: never film while the detail is a bill
        return 1
    print(f"model={r.model_id} tokens_in={r.tokens_in} tokens_out={r.tokens_out} ms={r.ms} "
          f"finish={r.finish_reason} reply={r.text!r}")
    return 0


if __name__ == "__main__":
    load_env()
    sys.exit(main())
