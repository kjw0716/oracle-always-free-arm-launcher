"""Offline contract/failure tests. Every OCI API is mocked; no live cloud writes."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "grab.py"
spec = importlib.util.spec_from_file_location("grab", SCRIPT)
g = importlib.util.module_from_spec(spec)
sys.modules["grab"] = g
spec.loader.exec_module(g)


def settings(**overrides):
    values = dict(auth={"tenancy": "ocid1.tenancy.oc1..test", "region": "us-sanjose-1"},
                  name="oci-a1", ad="AD1", vcn_name="example-vcn",
                  subnet_name="public subnet-example-vcn", subnet_id="", image_id="",
                  public_key="ssh-ed25519 dGVzdA== test", ocpus=2, memory=12, disk=50,
                  interval=120, jitter=30, max_backoff=900, capacity_first=False)
    values.update(overrides)
    return g.Settings(**values)


def instance(state="RUNNING", name="oci-a1", memory=12):
    return NS(id="ocid1.instance.oc1.sjc.test", lifecycle_state=state, display_name=name,
              shape=g.SHAPE, shape_config=NS(ocpus=2, memory_in_gbs=memory),
              compartment_id="ocid1.tenancy.oc1..test")


def error(status, code, message):
    return g.oci.exceptions.ServiceError(status, code, {}, message)


def backend():
    b = Mock()
    b.instances.return_value = []
    b.public_ip.return_value = "192.0.2.10"
    b.launch.return_value = instance()
    b.instance.return_value = instance()
    b.capacity.return_value = True
    b.request = {"availability_domain": "AD1", "image": "fixed-image", "name": "fixed-name"}
    return b


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s, self.b = settings(), backend()

    def runner(self, s=None):
        return g.Runner(s or self.s, self.b, self.tmp.name, clock=lambda: 100000)

    def state(self):
        return json.loads((Path(self.tmp.name) / "state.json").read_text(encoding="utf-8"))

    def test_capacity_does_not_mark_success_or_leave_uncertain_request(self):
        self.b.launch.side_effect = error(500, "InternalError", "Out of capacity")
        with self.assertRaises(g.oci.exceptions.ServiceError):
            self.runner().step()
        self.assertEqual(self.state()["status"], "out_of_capacity")
        self.assertNotIn("pending", self.state())

    def test_timeout_restart_reuses_exact_request_and_token(self):
        self.b.launch.side_effect = [TimeoutError(), instance()]
        with self.assertRaises(TimeoutError):
            self.runner().step()
        original = copy.deepcopy(self.state()["pending"])
        self.b.request = {"image": "new-image-after-restart"}
        self.assertEqual(self.runner().step(), "complete")
        args = self.b.launch.call_args_list
        self.assertEqual(args[0], args[1])
        self.assertEqual(args[1].args, (original["request"], original["token"]))

    def test_generic_server_error_preserves_pending_token(self):
        self.b.launch.side_effect = error(500, "InternalError", "unspecified")
        with self.assertRaises(g.oci.exceptions.ServiceError):
            self.runner().step()
        self.assertIn("pending", self.state())

    def test_accepted_request_persisted_before_ip_failure(self):
        self.b.public_ip.side_effect = [TimeoutError(), "192.0.2.10"]
        with self.assertRaises(TimeoutError):
            self.runner().step()
        self.assertEqual(self.state()["instance_id"], instance().id)
        self.assertEqual(self.runner().step(), "complete")
        self.assertEqual(self.b.launch.call_count, 1)

    def test_existing_provisioning_instance_never_launched_again(self):
        self.b.instances.return_value = [instance("PROVISIONING")]
        self.assertEqual(self.runner().step(), "provisioning")
        self.b.launch.assert_not_called()
        self.assertEqual(self.runner().step(), "complete")
        self.b.launch.assert_not_called()

    def test_existing_wrong_shape_config_is_not_adopted(self):
        self.b.instances.return_value = [instance(memory=24)]
        with self.assertRaises(g.StopError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_multiple_matches_stop(self):
        self.b.instances.return_value = [instance(), instance()]
        with self.assertRaises(g.StopError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_failure_listing_instances_is_not_treated_as_empty(self):
        self.b.instances.side_effect = error(403, "NotAuthorized", "denied")
        with self.assertRaises(g.oci.exceptions.ServiceError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_successful_restart_makes_no_api_calls(self):
        self.assertEqual(self.runner().step(), "complete")
        self.b.reset_mock()
        self.assertEqual(self.runner().step(), "complete")
        self.assertEqual(self.b.mock_calls, [])

    def test_unknown_request_near_token_expiry_stops(self):
        r = self.runner()
        r.state["pending"] = {"created_at": 100000 - 23 * 3600, "token": "token", "request": {}}
        r.save()
        with self.assertRaises(g.StopError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_config_change_with_state_stops(self):
        r = self.runner()
        r.save()
        with self.assertRaises(g.StopError):
            self.runner(settings(memory=6))

    def test_capacity_gate_never_launches_when_full(self):
        self.b.capacity.return_value = False
        self.assertEqual(self.runner(settings(capacity_first=True)).step(), "capacity")
        self.b.launch.assert_not_called()

    def test_stopped_target_is_not_replaced(self):
        self.b.instances.return_value = [instance("STOPPED")]
        with self.assertRaises(g.StopError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_usage_failure_prevents_launch(self):
        self.b.check_usage.side_effect = g.StopError("quota")
        with self.assertRaises(g.StopError):
            self.runner().step()
        self.b.launch.assert_not_called()

    def test_state_is_durable_before_cloud_launch(self):
        def launch(request, token):
            pending = self.state()["pending"]
            self.assertEqual(pending["token"], token)
            self.assertEqual(pending["request"], request)
            return instance()
        self.b.launch.side_effect = launch
        self.assertEqual(self.runner().step(), "complete")


class ContractTests(unittest.TestCase):
    def bare_oracle(self):
        o = object.__new__(g.Oracle)
        o.s = settings()
        o.root = o.s.auth["tenancy"]
        o.compute, o.storage, o.identity, o.network = Mock(), Mock(), Mock(), Mock()
        o.ads, o.compartments = [NS(name="AD1")], [o.root]
        o.all = lambda method, *args, **kwargs: method(*args, **kwargs).data
        return o

    def test_error_categories_distinguish_capacity_from_generic_500(self):
        cases = [(500, "InternalError", "Out of host capacity", "capacity"),
                 (500, "InternalError", "unrelated failure", "transient"),
                 (429, "TooManyRequests", "slow down", "throttle"),
                 (400, "LimitExceeded", "limit", "fatal"),
                 (401, "NotAuthenticated", "auth", "fatal"),
                 (404, "NotAuthorizedOrNotFound", "missing", "fatal")]
        for status, code, msg, expected in cases:
            with self.subTest(code=code, message=msg):
                self.assertEqual(g.classify_error(error(status, code, msg)), expected)

    def test_capacity_uses_official_status_not_nonexistent_available_boolean(self):
        o = self.bare_oracle()
        o.request = {"availability_domain": "AD1"}
        for status, expected in [("AVAILABLE", True), ("OUT_OF_HOST_CAPACITY", False)]:
            o.compute.create_compute_capacity_report.return_value.data = NS(shape_availabilities=[NS(availability_status=status)])
            self.assertEqual(o.capacity(), expected)
        o.compute.create_compute_capacity_report.return_value.data = NS(shape_availabilities=[])
        with self.assertRaises(g.StopError):
            o.capacity()

    def test_usage_counts_stopped_a1(self):
        o = self.bare_oracle()
        with self.assertRaises(g.StopError):
            o.check_usage([instance("STOPPED", name="another")])

    def test_volume_quota_counts_unattached_boot_volumes(self):
        o = self.bare_oracle()
        o.storage.list_volumes.return_value.data = [NS(size_in_gbs=100, lifecycle_state="AVAILABLE")]
        o.storage.list_boot_volumes.return_value.data = [NS(size_in_gbs=51, lifecycle_state="AVAILABLE")]
        with self.assertRaises(g.StopError):
            o.check_usage([])
        o.storage.list_boot_volumes.return_value.data[0].size_in_gbs = 50
        o.check_usage([])

    def test_real_sdk_models_serialize_launch_and_retry_token(self):
        o = self.bare_oracle()
        request = dict(compartment_id=o.root, availability_domain="AD1", display_name=o.s.name,
                       shape=g.SHAPE, shape_config={"ocpus": 2, "memory_in_gbs": 12},
                       source_details={"source_type": "image", "image_id": "image", "boot_volume_size_in_gbs": 50,
                                       "boot_volume_vpus_per_gb": 10},
                       create_vnic_details={"subnet_id": "subnet", "assign_public_ip": True},
                       metadata={"ssh_authorized_keys": o.s.public_key}, is_pv_encryption_in_transit_enabled=True)
        o.launch(request, "persisted-token")
        call = o.compute.launch_instance.call_args
        body = g.oci.util.to_dict(call.args[0])
        self.assertEqual(body["shape_config"]["memory_in_gbs"], 12)
        self.assertEqual(body["source_details"]["boot_volume_vpus_per_gb"], 10)
        self.assertEqual(call.kwargs["opc_retry_token"], "persisted-token")
        self.assertIsNone(body["fault_domain"])
        self.assertNotIn("user_data", body["metadata"])

    def test_lock_blocks_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            code = ("import sys;sys.path.insert(0,sys.argv[1]);import grab;"
                    "ctx=grab.single_process(sys.argv[2]);ctx.__enter__();ctx.__exit__(None,None,None)")
            with g.single_process(path):
                p = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(path)], capture_output=True)
                self.assertNotEqual(p.returncode, 0)
            p = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(path)], capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_preflight_discovery_uses_exact_existing_network_and_official_image(self):
        o = self.bare_oracle()
        o.identity.get_tenancy.return_value.data = NS(home_region_key="SJC")
        o.identity.list_regions.return_value.data = [NS(key="SJC", name="us-sanjose-1")]
        o.identity.list_availability_domains.return_value.data = [NS(name="AD1")]
        o.identity.list_compartments.return_value.data = [NS(id="child", lifecycle_state="ACTIVE")]
        o.network.list_vcns.return_value.data = [NS(id="vcn", display_name="example-vcn", lifecycle_state="AVAILABLE")]
        o.network.list_subnets.return_value.data = [NS(id="subnet", display_name="public subnet-example-vcn", lifecycle_state="AVAILABLE")]
        o.network.get_subnet.return_value.data = NS(lifecycle_state="AVAILABLE", prohibit_public_ip_on_vnic=False,
             prohibit_internet_ingress=False, compartment_id=o.root, availability_domain=None)
        o.compute.list_images.return_value.data = [NS(id="image", lifecycle_state="AVAILABLE", compartment_id=None)]
        o.compute.list_shapes.return_value.data = [NS(shape=g.SHAPE)]
        o.preflight()
        self.assertEqual(o.request["source_details"]["image_id"], "image")
        self.assertEqual(o.compartments, [o.root, "child"])
        self.assertEqual(o.network.method_calls[-1][0], "get_subnet")
        o.identity.get_tenancy.return_value.data = NS(home_region_key="IAD")
        with self.assertRaises(g.StopError):
            o.preflight()

    def test_missing_auth_exits_before_any_clients_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("[oracle]\nregion=us-sanjose-1\n[instance]\n[retry]\n", encoding="utf-8")
            with patch.object(g, "Oracle") as constructor:
                self.assertEqual(g.main(["--config", str(path), "--check"]), 2)
                constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
