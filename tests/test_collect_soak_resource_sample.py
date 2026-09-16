from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_soak_resource_sample", ROOT / "scripts" / "collect-soak-resource-sample.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SampleCollectionError = MODULE.SampleCollectionError


class SoakResourceSampleCollectorTest(unittest.TestCase):
    def test_project_name_is_restricted_to_disposable_soak_projects(self) -> None:
        self.assertEqual(
            MODULE.validate_project_name("irlight-poc-soak-123-456"),
            "irlight-poc-soak-123-456",
        )
        for invalid in (
            "irlight-poc",
            "production",
            "irlight-poc-soak-../prod",
            "irlight-poc-soak-",
            "irlight-poc-soak-$HOME",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SampleCollectionError):
                    MODULE.validate_project_name(invalid)

    def test_parse_stats_aggregates_cpu_only(self) -> None:
        output = "\n".join(
            [
                json.dumps({"CPUPerc": "12.5%"}),
                json.dumps({"CPUPerc": "0.5%"}),
            ]
        )
        self.assertEqual(MODULE.parse_stats_output(output, 2), 13.0)

    def test_parse_stats_fails_closed_on_shape_count_and_numbers(self) -> None:
        with self.assertRaisesRegex(SampleCollectionError, "expected 2 Docker stats rows"):
            MODULE.parse_stats_output(json.dumps({"CPUPerc": "1%"}), 2)
        for value in (
            {},
            {"CPUPerc": 1},
            {"CPUPerc": "nan%"},
            {"CPUPerc": "-1%"},
            {"CPUPerc": "1"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(SampleCollectionError):
                    MODULE.parse_stats_output(json.dumps(value), 1)

    def test_parse_top_and_aggregate_process_observations(self) -> None:
        rows = MODULE.parse_top_output("101 Ssl\n102 Z\n103 R+\n")
        self.assertEqual(rows, [(101, "Ssl"), (102, "Z"), (103, "R+")])
        observations = {101: (1000, 5), 102: (2000, 1), 103: (3000, 7)}
        memory, processes, zombies, fds = MODULE.aggregate_processes(
            rows, observations.__getitem__
        )
        self.assertEqual((memory, processes, zombies, fds), (6000, 3, 1, 13))

    def test_process_aggregation_rejects_duplicate_pids_and_invalid_proc_values(self) -> None:
        with self.assertRaisesRegex(SampleCollectionError, "duplicate PID"):
            MODULE.aggregate_processes([(10, "S"), (10, "R")], lambda _: (1, 1))
        with self.assertRaisesRegex(SampleCollectionError, "RSS byte count"):
            MODULE.aggregate_processes([(10, "S")], lambda _: (True, 1))
        with self.assertRaisesRegex(SampleCollectionError, "file-descriptor count"):
            MODULE.aggregate_processes([(10, "S")], lambda _: (1, True))
        with self.assertRaisesRegex(SampleCollectionError, "unexpected docker top row"):
            MODULE.parse_top_output("not-a-pid S")

    def test_media_metrics_are_strict_and_unknown_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(SampleCollectionError, "media-metrics-file is required"):
            MODULE.load_media_metrics(None, allow_unmeasured=False)
        self.assertEqual(
            MODULE.load_media_metrics(None, allow_unmeasured=True),
            {
                "bitrate_bps": None,
                "av_sync_drift_ms": None,
                "timestamp_errors": 0,
                "unexpected_reconnects": 0,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            path.write_text(
                json.dumps(
                    {
                        "bitrate_bps": 3_500_000,
                        "av_sync_drift_ms": -4.5,
                        "timestamp_errors": 2,
                        "unexpected_reconnects": 1,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                MODULE.load_media_metrics(path, allow_unmeasured=False),
                {
                    "bitrate_bps": 3_500_000.0,
                    "av_sync_drift_ms": -4.5,
                    "timestamp_errors": 2,
                    "unexpected_reconnects": 1,
                },
            )

    def test_media_metrics_reject_duplicates_unknown_fields_and_bad_counters(self) -> None:
        cases = (
            '{"bitrate_bps":1,"bitrate_bps":2,"av_sync_drift_ms":0,"timestamp_errors":0,"unexpected_reconnects":0}',
            '{"bitrate_bps":1,"av_sync_drift_ms":0,"timestamp_errors":0,"unexpected_reconnects":0,"extra":1}',
            '{"bitrate_bps":1,"av_sync_drift_ms":0,"timestamp_errors":true,"unexpected_reconnects":0}',
            '{"bitrate_bps":NaN,"av_sync_drift_ms":0,"timestamp_errors":0,"unexpected_reconnects":0}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.json"
            for raw in cases:
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises(SampleCollectionError):
                        MODULE.load_media_metrics(path, allow_unmeasured=False)

    def test_compose_lookup_checks_all_four_services_and_running_state(self) -> None:
        replies = iter(
            [
                "id-media\n", "true\n",
                "id-continuity\n", "true\n",
                "id-ui\n", "true\n",
                "id-agent\n", "true\n",
            ]
        )
        commands: list[list[str]] = []

        def fake_run(argv: list[str], **_: object) -> str:
            commands.append(argv)
            return next(replies)

        with mock.patch.object(MODULE, "run_checked", side_effect=fake_run):
            result = MODULE.compose_service_ids(
                "irlight-poc-soak-12-34", Path("docker-compose.poc.yml")
            )
        self.assertEqual(set(result), set(MODULE.EXPECTED_SERVICES))
        self.assertEqual(len(commands), 8)
        for index in range(0, 8, 2):
            self.assertEqual(commands[index][0:2], ["docker", "compose"])
            self.assertEqual(commands[index + 1][0:2], ["docker", "inspect"])

    def test_compose_lookup_rejects_missing_stopped_or_duplicate_containers(self) -> None:
        with mock.patch.object(MODULE, "run_checked", return_value=""):
            with self.assertRaisesRegex(SampleCollectionError, "exactly one container"):
                MODULE.compose_service_ids("irlight-poc-soak-a", Path("compose.yml"))

        replies = iter(["id-a\n", "false\n"])
        with mock.patch.object(MODULE, "run_checked", side_effect=lambda *a, **k: next(replies)):
            with self.assertRaisesRegex(SampleCollectionError, "is not running"):
                MODULE.compose_service_ids("irlight-poc-soak-a", Path("compose.yml"))

        replies = iter(
            [
                "same\n", "true\n",
                "same\n", "true\n",
                "id-c\n", "true\n",
                "id-d\n", "true\n",
            ]
        )
        with mock.patch.object(MODULE, "run_checked", side_effect=lambda *a, **k: next(replies)):
            with self.assertRaisesRegex(SampleCollectionError, "duplicate container IDs"):
                MODULE.compose_service_ids("irlight-poc-soak-a", Path("compose.yml"))

    def test_build_sample_matches_validator_schema_and_rejects_impossible_zombies(self) -> None:
        media = {
            "bitrate_bps": 1_000_000.0,
            "av_sync_drift_ms": 2.0,
            "timestamp_errors": 0,
            "unexpected_reconnects": 0,
        }
        sample = MODULE.build_sample(
            elapsed_seconds=30,
            memory_rss_bytes=100,
            cpu_percent=5.0,
            open_fds=10,
            processes=4,
            zombies=1,
            media_metrics=media,
        )
        self.assertEqual(
            set(sample),
            {
                "elapsed_seconds",
                "memory_rss_bytes",
                "cpu_percent",
                "open_fds",
                "processes",
                "zombies",
                "bitrate_bps",
                "av_sync_drift_ms",
                "timestamp_errors",
                "unexpected_reconnects",
            },
        )
        with self.assertRaisesRegex(SampleCollectionError, "zombies cannot exceed"):
            MODULE.build_sample(
                elapsed_seconds=0,
                memory_rss_bytes=0,
                cpu_percent=0,
                open_fds=0,
                processes=1,
                zombies=2,
                media_metrics=media,
            )

    def test_collect_sample_merges_read_only_resource_and_media_observations(self) -> None:
        service_ids = {
            "mediamtx": "a",
            "continuity": "b",
            "control-ui": "c",
            "node-agent": "d",
        }
        media = {
            "bitrate_bps": 4_000_000.0,
            "av_sync_drift_ms": 3.0,
            "timestamp_errors": 0,
            "unexpected_reconnects": 0,
        }
        with (
            mock.patch.object(MODULE, "compose_service_ids", return_value=service_ids),
            mock.patch.object(MODULE, "collect_cpu_percent", return_value=17.5),
            mock.patch.object(
                MODULE, "collect_process_observations", return_value=(1234, 9, 0, 80)
            ),
            mock.patch.object(MODULE, "load_media_metrics", return_value=media),
        ):
            sample = MODULE.collect_sample(
                project="irlight-poc-soak-x",
                compose_file=Path("compose.yml"),
                elapsed_seconds=60,
                media_metrics_file=Path("media.json"),
                allow_unmeasured_media=False,
            )
        self.assertEqual(sample["memory_rss_bytes"], 1234)
        self.assertEqual(sample["cpu_percent"], 17.5)
        self.assertEqual(sample["open_fds"], 80)
        self.assertEqual(sample["bitrate_bps"], 4_000_000.0)


if __name__ == "__main__":
    unittest.main()
