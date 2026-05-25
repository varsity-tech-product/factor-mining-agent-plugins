import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.error import URLError


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from factor_mining_agent_lib import cli
from factor_mining_agent_lib.config import AgentConfig, DEFAULT_BASE_URL, load_config, save_config
from factor_mining_agent_lib.run_state import RunState, save_run_state
from test_api_client import FakeOpener, FakeResponse


class CliTests(unittest.TestCase):
    def _save_config(self, home, secret="vt_test_secret_1234567890abcdef", agent_status=None):
        save_config(
            AgentConfig(
                base_url="https://factor.example",
                api_key=secret,
                agent_status=agent_status if agent_status is not None else {
                    "ok": True,
                    "mode": "local_agent",
                    "key_purpose": "external_agent",
                },
            ),
            home=Path(home),
        )

    def _save_run_state(self, home, client_run_id="run_123"):
        save_run_state(
            RunState(
                client_run_id=client_run_id,
                session_id="session_1",
                plugin_id="plugin_1",
                job_ids=["job_1"],
                plugin_path="/workspace/plugin.py",
                workflow_stage="submitted",
            ),
            home=Path(home),
        )

    def _task_payload(self):
        return {
            "task_id": "custom_liquidity_stress",
            "title": "Liquidity Stress",
            "category": "custom",
            "description": "Identify fragile liquidity regimes.",
            "allowed_data": ["close", "volume"],
            "fwd_period": 7,
            "hints": ["Use volume confirmation."],
        }

    def test_setup_uses_env_key_and_redacts_output(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--base-url", "https://factor.example", "--home", tmp],
                env={"FACTOR_MINING_AGENT_API_KEY": secret},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )
            config = load_config(home=Path(tmp))

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(config.api_key, secret)
        self.assertNotIn(secret, output)
        self.assertIn("vt_...cdef", output)
        self.assertEqual(config.agent_status["key_purpose"], "external_agent")

    def test_setup_uses_production_base_url_by_default(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--home", tmp],
                env={"FACTOR_MINING_AGENT_API_KEY": secret},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )
            config = load_config(home=Path(tmp))

        self.assertEqual(code, 0)
        self.assertEqual(config.base_url, DEFAULT_BASE_URL)
        self.assertEqual(opener.requests[0].full_url, f"{DEFAULT_BASE_URL}/health")
        self.assertEqual(opener.requests[1].full_url, f"{DEFAULT_BASE_URL}/agent/status")

    def test_setup_accepts_base_url_override(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--base-url", "https://staging.factor.example", "--home", tmp],
                env={"FACTOR_MINING_AGENT_API_KEY": secret},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                opener=opener,
            )
            config = load_config(home=Path(tmp))

        self.assertEqual(code, 0)
        self.assertEqual(config.base_url, "https://staging.factor.example")
        self.assertEqual(opener.requests[0].full_url, "https://staging.factor.example/health")

    def test_setup_rejects_non_external_agent_status(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "web", "key_purpose": "user"}),
            ]
        )
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--base-url", "https://factor.example", "--home", tmp],
                env={"FACTOR_MINING_AGENT_API_KEY": secret},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

            self.assertEqual(code, 1)
            self.assertFalse((Path(tmp) / "config.json").exists())
            self.assertIn("external_agent", stderr.getvalue())
            self.assertIn("Agent API Key", stderr.getvalue())
            self.assertNotIn(secret, stderr.getvalue())

    def test_setup_has_no_public_skip_verify_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        ["setup", "--base-url", "https://factor.example", "--skip-verify", "--home", tmp],
                        env={"FACTOR_MINING_AGENT_API_KEY": "vt_test_secret_1234567890abcdef"},
                        stdin=io.StringIO(),
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                        opener=FakeOpener([]),
                    )

        self.assertEqual(raised.exception.code, 2)

    def test_setup_can_read_key_from_non_echo_stdin(self):
        secret = "vt_test_secret_abcdef1234567890"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--base-url", "https://factor.example", "--api-key-stdin", "--home", tmp],
                env={},
                stdin=io.StringIO(secret + "\n"),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )
            config = load_config(home=Path(tmp))

        self.assertEqual(code, 0)
        self.assertEqual(config.api_key, secret)
        self.assertNotIn(secret, stdout.getvalue())

    def test_status_reads_config_and_never_prints_secret_on_error(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener([FakeResponse(status=401, body={"detail": f"bad key {secret}"})])
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp, secret=secret)
            code = cli.main(
                ["status", "--home", tmp],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("vt_...cdef", stderr.getvalue())

    def test_status_rejects_live_non_external_agent_status(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(body={"ok": True, "mode": "web", "key_purpose": "user"}),
            ]
        )
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            code = cli.main(
                ["status", "--home", tmp],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertIn("external_agent", stderr.getvalue())
        self.assertIn("Agent API Key", stderr.getvalue())

    def test_setup_reports_missing_agent_status_endpoint_clearly(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener(
            [
                FakeResponse(body={"db": "ok"}),
                FakeResponse(status=404, body={"detail": "not found"}),
            ]
        )
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["setup", "--base-url", "https://factor.example", "--home", tmp],
                env={"FACTOR_MINING_AGENT_API_KEY": secret},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertIn("external-agent status endpoint", stderr.getvalue())
        self.assertIn("Factor Mining API environment", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_authenticated_command_refuses_unverified_config_without_api_call(self):
        secret = "vt_test_secret_1234567890abcdef"
        opener = FakeOpener([FakeResponse(body={"tasks": []})])
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp, secret=secret, agent_status={})
            code = cli.main(
                ["tasks", "--home", tmp],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertEqual(opener.requests, [])
        self.assertIn("Run setup", stderr.getvalue())
        self.assertIn("Agent API Key", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_authenticated_command_validates_live_status_before_api_action(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"tasks": []}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            code = cli.main(
                ["tasks", "--home", tmp],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )

        self.assertEqual(code, 0)
        self.assertEqual(opener.requests[0].full_url, "https://factor.example/agent/status")
        self.assertEqual(opener.requests[1].full_url, "https://factor.example/tasks?limit=20&status=open")
        self.assertEqual(json.loads(stdout.getvalue()), {"tasks": []})

    def test_create_session_accepts_task_payload_json_for_custom_session(self):
        task_payload = self._task_payload()
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"session_id": "session_1"}),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            code = cli.main(
                [
                    "create-session",
                    "--home",
                    tmp,
                    "--idea",
                    "liquidity stress",
                    "--client-run-id",
                    "run_123",
                    "--task-payload-json",
                    json.dumps(task_payload),
                ],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                opener=opener,
            )

        self.assertEqual(code, 0)
        body = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual(body["origin"], "custom")
        self.assertEqual(body["idea"], "liquidity stress")
        self.assertEqual(body["task_payload"], task_payload)

    def test_create_session_accepts_task_payload_file(self):
        task_payload = self._task_payload()
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"session_id": "session_1"}),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            task_payload_path = Path(tmp) / "task_payload.json"
            task_payload_path.write_text(json.dumps(task_payload), encoding="utf-8")
            code = cli.main(
                [
                    "create-session",
                    "--home",
                    tmp,
                    "--idea",
                    "liquidity stress",
                    "--task-payload-file",
                    str(task_payload_path),
                ],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                opener=opener,
            )

        self.assertEqual(code, 0)
        body = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual(body["task_payload"], task_payload)

    def test_create_custom_session_requires_direct_task_payload_before_session_call(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"session_id": "session_1"}),
            ]
        )
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            code = cli.main(
                [
                    "create-session",
                    "--home",
                    tmp,
                    "--idea",
                    "liquidity stress",
                    "--client-run-id",
                    "run_123",
                ],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("task_payload", stderr.getvalue())
        self.assertIn("--task-payload-file", stderr.getvalue())

    def test_create_session_rejects_invalid_direct_task_payload_before_session_call(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"session_id": "session_1"}),
            ]
        )
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            code = cli.main(
                [
                    "create-session",
                    "--home",
                    tmp,
                    "--idea",
                    "liquidity stress",
                    "--task-payload-json",
                    json.dumps({"task_id": "custom_liquidity_stress"}),
                ],
                env={},
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("task_payload", stderr.getvalue())
        self.assertIn("allowed_data", stderr.getvalue())

    def test_resume_continues_on_missing_or_expired_artifact(self):
        for status in (404, 410):
            with self.subTest(status=status):
                opener = FakeOpener(
                    [
                        FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                        FakeResponse(body={"stage": "done"}),
                        FakeResponse(body={"job_id": "job_1", "status": "succeeded"}),
                        FakeResponse(status=status, body={"detail": "artifact unavailable"}),
                    ]
                )
                stdout = io.StringIO()

                with tempfile.TemporaryDirectory() as tmp:
                    self._save_config(tmp)
                    self._save_run_state(tmp)
                    code = cli.main(
                        ["resume", "--home", tmp, "--client-run-id", "run_123"],
                        env={},
                        stdin=io.StringIO(),
                        stdout=stdout,
                        stderr=io.StringIO(),
                        opener=opener,
                    )

                payload = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(payload["artifact"]["status"], "unavailable")
                self.assertEqual(payload["artifact"]["errors"][0]["status"], status)
                self.assertEqual(payload["summary"]["jobs"][0]["status"], "succeeded")

    def test_resume_fails_on_non_artifact_unavailable_errors(self):
        cases = [
            ("unauthorized", FakeResponse(status=401, body={"detail": "bad key"})),
            ("forbidden", FakeResponse(status=403, body={"detail": "forbidden"})),
            ("server", FakeResponse(status=500, body={"detail": "server error"})),
            ("network", URLError("network down")),
            ("unexpected", RuntimeError("unexpected artifact failure")),
        ]

        for _, artifact_response in cases:
            with self.subTest(response=artifact_response):
                opener = FakeOpener(
                    [
                        FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                        FakeResponse(body={"stage": "done"}),
                        FakeResponse(body={"job_id": "job_1", "status": "succeeded"}),
                        artifact_response,
                    ]
                )
                stdout = io.StringIO()
                stderr = io.StringIO()

                with tempfile.TemporaryDirectory() as tmp:
                    self._save_config(tmp)
                    self._save_run_state(tmp)
                    code = cli.main(
                        ["resume", "--home", tmp, "--client-run-id", "run_123"],
                        env={},
                        stdin=io.StringIO(),
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )

                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertTrue(stderr.getvalue())

    def test_artifact_output_name_is_sanitized_before_writing(self):
        unsafe_names = ["", "../card.json", "nested/card.json", "/absolute/card.json", ".."]

        for name in unsafe_names:
            with self.subTest(name=name):
                opener = FakeOpener(
                    [
                        FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                    ]
                )
                stderr = io.StringIO()
                with tempfile.TemporaryDirectory() as tmp:
                    self._save_config(tmp)
                    code = cli.main(
                        [
                            "artifact",
                            "--home",
                            tmp,
                            "--job-id",
                            "job_1",
                            "--name",
                            name,
                            "--output-dir",
                            str(Path(tmp) / "artifacts"),
                        ],
                        env={},
                        stdin=io.StringIO(),
                        stdout=io.StringIO(),
                        stderr=stderr,
                        opener=opener,
                    )

                self.assertEqual(code, 1)
                self.assertEqual(len(opener.requests), 1)
                self.assertIn("artifact name", stderr.getvalue())

    def test_metadata_command_outputs_compact_json(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = Path(tmp) / "plugin.py"
            plugin_path.write_text(
                "\n".join(
                    [
                        'FACTOR_TYPE = "alpha"',
                        'FACTOR_NAME = "Liquidity Stress"',
                        'FACTOR_DEFAULT_PARAMS = {"lookback": 48}',
                    ]
                ),
                encoding="utf-8",
            )
            code = cli.main(
                ["metadata", "--plugin-path", str(plugin_path)],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=FakeOpener([]),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["factor_name"], "Liquidity Stress")
        self.assertEqual(payload["params"], {"lookback": 48})

    def test_upload_backtest_wait_polls_until_terminal_and_summarizes_artifact(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"plugin_id": "plugin_1"}),
                FakeResponse(body={"job_ids": ["job_1"], "jobs": [{"job_id": "job_1", "position_mode": "cs_only"}]}),
                FakeResponse(body={"stage": "backtesting", "status": "running"}),
                FakeResponse(body={"job_id": "job_1", "status": "queued", "position_mode": "cs_only"}),
                FakeResponse(body={"stage": "done", "status": "done"}),
                FakeResponse(
                    body={
                        "job_id": "job_1",
                        "status": "done",
                        "position_mode": "cs_only",
                        "factor_card_summary": {"composite_sharpe": 1.4, "cs_success": True},
                        "factor_card_ready": True,
                    }
                ),
                FakeResponse(
                    body={
                        "factor_name": "Liquidity Stress",
                        "metrics": {"composite_sharpe": 1.4},
                        "artifacts": {"default_factor_card.json": "available"},
                    }
                ),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            plugin_path = Path(tmp) / "plugin.py"
            plugin_path.write_text(
                "\n".join(
                    [
                        'FACTOR_TYPE = "alpha"',
                        'FACTOR_NAME = "Liquidity Stress"',
                        'FACTOR_DEFAULT_PARAMS = {"lookback": 48}',
                    ]
                ),
                encoding="utf-8",
            )
            code = cli.main(
                [
                    "upload-backtest",
                    "--home",
                    tmp,
                    "--session-id",
                    "session_1",
                    "--plugin-path",
                    str(plugin_path),
                    "--client-run-id",
                    "run_123",
                    "--wait",
                    "--poll-interval",
                    "0",
                    "--timeout",
                    "5",
                ],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["terminal_status"], "succeeded")
        self.assertEqual(payload["client_run_id"], "run_123")
        self.assertEqual(payload["session_id"], "session_1")
        self.assertEqual(payload["plugin_id"], "plugin_1")
        self.assertEqual(payload["job_ids"], ["job_1"])
        self.assertEqual(payload["jobs"][0]["status"], "done")
        self.assertEqual(payload["workflow"]["stage"], "done")
        self.assertEqual(payload["artifact"]["status"], "available")
        self.assertEqual(payload["summary"]["factor_name"], "Liquidity Stress")
        upload_body = opener.requests[1].data.decode("utf-8")
        self.assertIn('name="fwd_period"', upload_body)
        self.assertIn("\r\n7\r\n", upload_body)

    def test_upload_backtest_wait_reports_failed_terminal_jobs_clearly(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"plugin_id": "plugin_1"}),
                FakeResponse(body={"job_ids": ["job_1"]}),
                FakeResponse(body={"stage": "done", "status": "done"}),
                FakeResponse(
                    body={
                        "job_id": "job_1",
                        "status": "failed",
                        "position_mode": "cs_only",
                        "failure_reason": "validation failed",
                    }
                ),
                FakeResponse(body={"factor_name": "Liquidity Stress", "metrics": {}}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            plugin_path = Path(tmp) / "plugin.py"
            plugin_path.write_text(
                'FACTOR_TYPE = "alpha"\nFACTOR_NAME = "Liquidity Stress"\nFACTOR_DEFAULT_PARAMS = {}\n',
                encoding="utf-8",
            )
            code = cli.main(
                [
                    "upload-backtest",
                    "--home",
                    tmp,
                    "--session-id",
                    "session_1",
                    "--plugin-path",
                    str(plugin_path),
                    "--client-run-id",
                    "run_failed",
                    "--wait",
                    "--poll-interval",
                    "0",
                    "--timeout",
                    "5",
                ],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["terminal_status"], "failed")
        self.assertEqual(payload["failures"][0]["job_id"], "job_1")
        self.assertEqual(payload["summary"]["failures"][0]["status"], "failed")

    def test_upload_backtest_wait_reports_unavailable_artifact_on_404_or_410(self):
        for status in (404, 410):
            with self.subTest(status=status):
                opener = FakeOpener(
                    [
                        FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                        FakeResponse(body={"plugin_id": "plugin_1"}),
                        FakeResponse(body={"job_ids": ["job_1"]}),
                        FakeResponse(body={"stage": "done", "status": "done"}),
                        FakeResponse(body={"job_id": "job_1", "status": "done", "position_mode": "cs_only"}),
                        FakeResponse(status=status, body={"detail": "artifact unavailable"}),
                    ]
                )
                stdout = io.StringIO()

                with tempfile.TemporaryDirectory() as tmp:
                    self._save_config(tmp)
                    plugin_path = Path(tmp) / "plugin.py"
                    plugin_path.write_text(
                        'FACTOR_TYPE = "alpha"\nFACTOR_NAME = "Liquidity Stress"\nFACTOR_DEFAULT_PARAMS = {}\n',
                        encoding="utf-8",
                    )
                    code = cli.main(
                        [
                            "upload-backtest",
                            "--home",
                            tmp,
                            "--session-id",
                            "session_1",
                            "--plugin-path",
                            str(plugin_path),
                            "--client-run-id",
                            "run_123",
                            "--wait",
                            "--poll-interval",
                            "0",
                            "--timeout",
                            "5",
                        ],
                        env={},
                        stdin=io.StringIO(),
                        stdout=stdout,
                        stderr=io.StringIO(),
                        opener=opener,
                    )

                payload = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["status"], "succeeded")
                self.assertEqual(payload["artifact"]["status"], "unavailable")
                self.assertEqual(payload["artifact"]["errors"][0]["status"], status)

    def test_upload_backtest_wait_fails_on_artifact_auth_server_network_or_unexpected_errors(self):
        cases = [
            FakeResponse(status=401, body={"detail": "bad key"}),
            FakeResponse(status=403, body={"detail": "forbidden"}),
            FakeResponse(status=500, body={"detail": "server error for vt_test_secret_1234567890abcdef"}),
            URLError("network down"),
            RuntimeError("unexpected artifact failure"),
        ]

        for artifact_response in cases:
            with self.subTest(response=artifact_response):
                opener = FakeOpener(
                    [
                        FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                        FakeResponse(body={"plugin_id": "plugin_1"}),
                        FakeResponse(body={"job_ids": ["job_1"]}),
                        FakeResponse(body={"stage": "done", "status": "done"}),
                        FakeResponse(body={"job_id": "job_1", "status": "done", "position_mode": "cs_only"}),
                        artifact_response,
                    ]
                )
                stdout = io.StringIO()
                stderr = io.StringIO()

                with tempfile.TemporaryDirectory() as tmp:
                    self._save_config(tmp)
                    plugin_path = Path(tmp) / "plugin.py"
                    plugin_path.write_text(
                        'FACTOR_TYPE = "alpha"\nFACTOR_NAME = "Liquidity Stress"\nFACTOR_DEFAULT_PARAMS = {}\n',
                        encoding="utf-8",
                    )
                    code = cli.main(
                        [
                            "upload-backtest",
                            "--home",
                            tmp,
                            "--session-id",
                            "session_1",
                            "--plugin-path",
                            str(plugin_path),
                            "--client-run-id",
                            "run_123",
                            "--wait",
                            "--poll-interval",
                            "0",
                            "--timeout",
                            "5",
                        ],
                        env={},
                        stdin=io.StringIO(),
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )

                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertTrue(stderr.getvalue())
                self.assertNotIn("vt_test_secret_1234567890abcdef", stderr.getvalue())

    def test_upload_backtest_wait_timeout_returns_resume_guidance(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"plugin_id": "plugin_1"}),
                FakeResponse(body={"job_ids": ["job_1"]}),
                FakeResponse(body={"stage": "backtesting", "status": "running"}),
                FakeResponse(body={"job_id": "job_1", "status": "queued", "position_mode": "cs_only"}),
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            plugin_path = Path(tmp) / "plugin.py"
            plugin_path.write_text(
                'FACTOR_TYPE = "alpha"\nFACTOR_NAME = "Liquidity Stress"\nFACTOR_DEFAULT_PARAMS = {}\n',
                encoding="utf-8",
            )
            code = cli.main(
                [
                    "upload-backtest",
                    "--home",
                    tmp,
                    "--session-id",
                    "session_1",
                    "--plugin-path",
                    str(plugin_path),
                    "--client-run-id",
                    "run_timeout",
                    "--wait",
                    "--poll-interval",
                    "0",
                    "--timeout",
                    "0",
                ],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Backtest timed out", stderr.getvalue())
        self.assertIn("factor_api.py resume --client-run-id run_timeout --wait", stderr.getvalue())

    def test_resume_wait_polls_until_terminal_and_writes_artifact(self):
        opener = FakeOpener(
            [
                FakeResponse(body={"ok": True, "mode": "local_agent", "key_purpose": "external_agent"}),
                FakeResponse(body={"stage": "backtesting", "status": "running"}),
                FakeResponse(body={"job_id": "job_1", "status": "queued", "position_mode": "cs_only"}),
                FakeResponse(body={"stage": "done", "status": "done"}),
                FakeResponse(body={"job_id": "job_1", "status": "done", "position_mode": "cs_only"}),
                FakeResponse(body={"factor_name": "Liquidity Stress", "metrics": {"composite_sharpe": 1.2}}),
            ]
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            self._save_config(tmp)
            self._save_run_state(tmp, client_run_id="run_resume")
            output_dir = Path(tmp) / "artifacts"
            code = cli.main(
                [
                    "resume",
                    "--home",
                    tmp,
                    "--client-run-id",
                    "run_resume",
                    "--wait",
                    "--poll-interval",
                    "0",
                    "--timeout",
                    "5",
                    "--output-dir",
                    str(output_dir),
                ],
                env={},
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                opener=opener,
            )

            payload = json.loads(stdout.getvalue())
            artifact_path = output_dir / "default_factor_card.json"

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["artifact"]["path"], str(artifact_path))
            self.assertTrue(artifact_path.exists())


if __name__ == "__main__":
    unittest.main()
