import json
from typing import List, Optional

from injector import inject

from taskweaver.code_interpreter.code_executor import CodeExecutor
from taskweaver.code_interpreter.code_generator import CodeGeneratorPluginOnly
from taskweaver.config.module_config import ModuleConfig
from taskweaver.logging import TelemetryLogger
from taskweaver.memory import Memory, Post
from taskweaver.memory.attachment import AttachmentType
from taskweaver.module.event_emitter import SessionEventEmitter
from taskweaver.module.tracing import Tracing, get_tracer, tracing_decorator
from taskweaver.role import Role


class CodeInterpreterConfig(ModuleConfig):
    def _configure(self):
        self._set_name("code_interpreter_plugin_only")
        self.use_local_uri = self._get_bool("use_local_uri", False)
        self.max_retry_count = self._get_int("max_retry_count", 3)


class CodeInterpreterPluginOnly(Role):
    @inject
    def __init__(
        self,
        generator: CodeGeneratorPluginOnly,
        executor: CodeExecutor,
        logger: TelemetryLogger,
        tracing: Tracing,
        event_emitter: SessionEventEmitter,
        config: CodeInterpreterConfig,
    ):
        self.generator = generator
        self.executor = executor
        self.logger = logger
        self.tracing = tracing
        self.config = config
        self.event_emitter = event_emitter
        self.retry_count = 0
        self.return_index = 0

        self.logger.info("CodeInterpreter initialized successfully.")

    @tracing_decorator
    def reply(
        self,
        memory: Memory,
        prompt_log_path: Optional[str] = None,
        use_back_up_engine: bool = False,
    ) -> Post:
        post_proxy = self.event_emitter.create_post_proxy("CodeInterpreter")
        self.generator.reply(
            memory,
            post_proxy=post_proxy,
            prompt_log_path=prompt_log_path,
            use_back_up_engine=use_back_up_engine,
        )

        if post_proxy.post.message is not None and post_proxy.post.message != "":  # type: ignore
            return post_proxy.end()

        functions = json.loads(
            post_proxy.post.get_attachment(type=AttachmentType.function)[0],
        )
        if len(functions) > 0:
            code: List[str] = []
            for i, f in enumerate(functions):
                function_name = f["name"]
                function_args = f["arguments"]
                function_call = (
                    f"r{self.return_index + i}={function_name}("
                    + ", ".join(
                        [
                            f'{key}="{value}"' if isinstance(value, str) else f"{key}={value}"
                            for key, value in function_args.items()
                        ],
                    )
                    + ")"
                )
                code.append(function_call)
            code.append(
                f'{", ".join([f"r{self.return_index + i}" for i in range(len(functions))])}',
            )
            self.return_index += len(functions)

            code_to_exec = "\n".join(code)
            post_proxy.update_attachment(code_to_exec, AttachmentType.python)

            with get_tracer().start_span("executing_code") as span:
                span.set_tag("code", code_to_exec)

                exec_result = self.executor.execute_code(
                    exec_id=post_proxy.post.id,
                    code=code_to_exec,
                )

            code_output = self.executor.format_code_output(
                exec_result,
                with_code=True,
                use_local_uri=self.config.use_local_uri,
            )

            post_proxy.update_message(
                code_output,
                is_end=True,
            )

            if not exec_result.is_success:
                self.tracing.set_span_status("ERROR", "Code execution failed.")
            self.tracing.set_span_attribute("code_output", code_output)
        else:
            post_proxy.update_message(
                "No code is generated because no function is selected.",
            )

        reply_post = post_proxy.end()

        self.tracing.set_span_attribute("out.from", reply_post.send_from)
        self.tracing.set_span_attribute("out.to", reply_post.send_to)
        self.tracing.set_span_attribute("out.message", reply_post.message)
        self.tracing.set_span_attribute("out.attachments", str(reply_post.attachment_list))

        return reply_post
