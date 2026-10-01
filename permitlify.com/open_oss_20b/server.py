"""
OpenAI-compatible API server for gpt-oss-20b/120b.

Supports:
  - /v1/chat/completions  (OpenAI Chat Completions API)
  - /v1/responses         (OpenAI Responses API)
  - /v1/models            (list available models)

Usage:
  python server.py --model openai/gpt-oss-20b --port 8000
"""

import argparse
import datetime
import json
import os
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openai_harmony import (
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    Message,
    ReasoningEffort,
    Role,
    StreamableParser,
    StreamState,
    SystemContent,
    load_harmony_encoding,
)

MODEL_NAME = os.environ.get("MODEL_NAME", "openai/gpt-oss-20b")
DEFAULT_TEMPERATURE = 0.0


def create_app(infer_next_token, encoding, model_name: str = MODEL_NAME):
    app = FastAPI(title="GPT-OSS API")

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "openai",
                }
            ],
        }

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": str(exc),
                    "type": type(exc).__name__,
                }
            },
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        messages = body.get("messages", [])
        temperature = body.get("temperature", DEFAULT_TEMPERATURE)
        max_tokens = body.get("max_tokens", 256)
        stream = body.get("stream", False)

        # Build Harmony conversation from messages
        harmony_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                sys_content = SystemContent.new().with_reasoning_effort(
                    ReasoningEffort.LOW
                )
                harmony_messages.append(
                    Message.from_role_and_content(Role.SYSTEM, sys_content)
                )
            elif role == "developer":
                dev_content = DeveloperContent.new().with_instructions(content)
                harmony_messages.append(
                    Message.from_role_and_content(Role.DEVELOPER, dev_content)
                )
            elif role in ("user", "assistant"):
                m = Message.from_role_and_content(
                    Role.USER if role == "user" else Role.ASSISTANT,
                    content,
                )
                harmony_messages.append(m)

        if not any(m.author.role == Role.SYSTEM for m in harmony_messages):
            sys_content = SystemContent.new().with_reasoning_effort(
                ReasoningEffort.LOW
            )
            harmony_messages.insert(
                0, Message.from_role_and_content(Role.SYSTEM, sys_content)
            )

        conversation = Conversation.from_messages(harmony_messages)
        tokens = encoding.render_conversation_for_completion(
            conversation, Role.ASSISTANT
        )

        if stream:

            async def generate():
                output_tokens = []
                parser = StreamableParser(encoding, role=Role.ASSISTANT)
                new_request = True

                while len(output_tokens) < max_tokens:
                    next_tok = infer_next_token(
                        tokens + output_tokens,
                        temperature=temperature,
                        new_request=new_request,
                    )
                    new_request = False
                    output_tokens.append(next_tok)
                    parser.process(next_tok)

                    if next_tok in encoding.stop_tokens_for_assistant_actions():
                        break

                    if parser.last_content_delta and parser.current_channel == "final":
                        delta = parser.last_content_delta
                        chunk = {
                            "id": f"chatcmpl-{uuid.uuid4().hex}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield "data: " + json.dumps(chunk) + "\n\n"

                final_chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": len(tokens),
                        "completion_tokens": len(output_tokens),
                        "total_tokens": len(tokens) + len(output_tokens),
                    },
                }
                yield "data: " + json.dumps(final_chunk) + "\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            output_tokens = []
            new_request = True
            while len(output_tokens) < max_tokens:
                next_tok = infer_next_token(
                    tokens + output_tokens,
                    temperature=temperature,
                    new_request=new_request,
                )
                new_request = False
                output_tokens.append(next_tok)

                if next_tok in encoding.stop_tokens_for_assistant_actions():
                    break

            output_text = encoding.decode_utf8(output_tokens)

            return JSONResponse(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": output_text,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": len(tokens),
                        "completion_tokens": len(output_tokens),
                        "total_tokens": len(tokens) + len(output_tokens),
                    },
                }
            )

    @app.post("/v1/responses")
    async def responses_api(request: Request):
        body = await request.json()
        input_text = body.get("input", "")
        instructions = body.get("instructions", "")
        temperature = body.get("temperature", DEFAULT_TEMPERATURE)
        max_output_tokens = body.get("max_output_tokens", 256)

        harmony_messages = []
        sys_content = (
            SystemContent.new()
            .with_reasoning_effort(ReasoningEffort.LOW)
            .with_conversation_start_date(
                datetime.datetime.now().strftime("%Y-%m-%d")
            )
        )
        harmony_messages.append(
            Message.from_role_and_content(Role.SYSTEM, sys_content)
        )

        if instructions:
            dev_content = DeveloperContent.new().with_instructions(instructions)
            harmony_messages.append(
                Message.from_role_and_content(Role.DEVELOPER, dev_content)
            )

        harmony_messages.append(
            Message.from_role_and_content(Role.USER, input_text)
        )

        conversation = Conversation.from_messages(harmony_messages)
        tokens = encoding.render_conversation_for_completion(
            conversation, Role.ASSISTANT
        )

        output_tokens = []
        new_request = True
        while len(output_tokens) < max_output_tokens:
            next_tok = infer_next_token(
                tokens + output_tokens,
                temperature=temperature,
                new_request=new_request,
            )
            new_request = False
            output_tokens.append(next_tok)

            if next_tok in encoding.stop_tokens_for_assistant_actions():
                break

        output_text = encoding.decode_utf8(output_tokens)

        return JSONResponse(
            {
                "id": f"resp_{uuid.uuid4().hex}",
                "object": "response",
                "created_at": int(datetime.datetime.now().timestamp()),
                "status": "completed",
                "model": model_name,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": output_text,
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": len(tokens),
                    "output_tokens": len(output_tokens),
                    "total_tokens": len(tokens) + len(output_tokens),
                },
            }
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="GPT-OSS API Server")
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("MODEL_NAME", "openai/gpt-oss-20b"),
        help="Model name to report in API responses (default: openai/gpt-oss-20b)",
    )
    parser.add_argument(
        "--checkpoint",
        default=os.environ.get("CHECKPOINT", "openai/gpt-oss-20b"),
        help="HuggingFace model ID or local checkpoint path",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=int(os.environ.get("PORT", 8000)),
        help="Server port (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--inference-backend",
        choices=["transformers", "triton", "vllm", "ollama", "metal", "stub"],
        default="transformers",
        help="Inference backend (default: transformers)",
    )
    args = parser.parse_args()

    # Set model name from args or env
    model_name = args.model
    checkpoint = args.checkpoint

    # Load encoding
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)

    # Load model based on backend
    print(f"Loading model from: {checkpoint}")
    print(f"Using backend: {args.inference_backend}")
    print(f"Model name for API: {model_name}")

    if args.inference_backend == "transformers":
        from typing import List
        from transformers import AutoModelForCausalLM
        import torch

        print("Loading model (CPU, this may take a while)...")
        model = AutoModelForCausalLM.from_pretrained(
            checkpoint,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            offload_folder="./offload",
        )
        model.eval()
        device = next(model.parameters()).device

        def infer_next_token(tokens: List[int], temperature: float = 0.0, new_request: bool = False) -> int:
            input_ids = torch.tensor([tokens], dtype=torch.int64, device=device)
            with torch.no_grad():
                outputs = model.generate(
                    input_ids,
                    max_new_tokens=1,
                    do_sample=temperature != 0,
                    temperature=temperature if temperature != 0 else None,
                )
            return outputs[0, -1].item()

        print("Model loaded successfully!")
    elif args.inference_backend == "stub":
        from gpt_oss.responses_api.inference.stub import setup_model
        infer_next_token = setup_model(checkpoint)
    elif args.inference_backend == "ollama":
        from gpt_oss.responses_api.inference.ollama import setup_model
        infer_next_token = setup_model(checkpoint)
    elif args.inference_backend == "vllm":
        from gpt_oss.responses_api.inference.vllm import setup_model
        infer_next_token = setup_model(checkpoint)
    elif args.inference_backend == "triton":
        from gpt_oss.responses_api.inference.triton import setup_model
        infer_next_token = setup_model(checkpoint)
    elif args.inference_backend == "metal":
        from gpt_oss.responses_api.inference.metal import setup_model
        infer_next_token = setup_model(checkpoint)
    else:
        raise ValueError(f"Unsupported backend: {args.inference_backend}")

    app = create_app(infer_next_token, encoding, model_name=model_name)

    print(f"Server starting on http://{args.host}:{args.port}")
    print(f"Chat Completions: http://{args.host}:{args.port}/v1/chat/completions")
    print(f"Responses API:    http://{args.host}:{args.port}/v1/responses")
    print(f"Models:           http://{args.host}:{args.port}/v1/models")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
