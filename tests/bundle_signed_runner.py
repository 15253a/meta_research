from pathlib import Path
from meta_research.provider_supervisor import read_supervisor_request, read_transport_key_for_operation, write_exit_receipt
from test_bundle_skill_adapter import _SequenceRunner

class _SignedDispatchRunner(_SequenceRunner):
    def run_durable_job(self, job_ref, argv, prompt, timeout, stdout_path,
                        pid_path, request_path, environment=None):
        result = self(argv, prompt, timeout, environment)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        _, key = read_transport_key_for_operation(request_path.parent)
        sealed = read_supervisor_request(request_path, key)
        write_exit_receipt(
            Path(sealed["receipt_path"]), key=key,
            invocation_hash=sealed["invocation_hash"],
            prompt_path=Path(sealed["prompt_path"]),
            schema_path=Path(sealed["schema_path"]), stdout_path=stdout_path,
            result_path=Path(sealed["result_path"]), returncode=0,
            input_bytes=len(prompt.encode("utf-8")),
        )
        return result
