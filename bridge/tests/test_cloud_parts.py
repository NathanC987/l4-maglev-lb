"""The small pure pieces of the AWS canvas: what is accepted from AWS and from the
wire, and exactly what reaches a remote shell."""
import json
import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud_bridge as cb  # noqa: E402
import lb_agent  # noqa: E402


def inst(role, ip, iid="i-0123456789abcdef0", mac="0A:00:00:00:00:01", state="stopped"):
    return {"InstanceId": iid, "InstanceType": "t3.micro", "State": {"Name": state},
            "PrivateIpAddress": ip, "NetworkInterfaces": [{"MacAddress": mac}],
            "Tags": [{"Key": "l4mlb-role", "Value": role}]}


class ParseInstances(unittest.TestCase):
    def parse(self, *insts):
        return cb.parse_instances({"Reservations": [{"Instances": list(insts)}]})

    def test_normalises_the_mac(self):
        self.assertEqual(self.parse(inst("be1", "10.99.1.31"))[0]["mac"], "0a:00:00:00:00:01")

    def test_drops_anything_unexpected(self):
        self.assertEqual(self.parse(inst("mon", "10.99.1.40")), [])
        self.assertEqual(self.parse(inst("be9", "x")), [])
        self.assertEqual(self.parse(inst("be1; rm -rf /", "x")), [])
        self.assertEqual(self.parse(inst("be1", "x", iid="i-$(boom)")), [])
        self.assertEqual(self.parse(inst("be1", "x", mac="not-a-mac")), [])


class CheckPool(unittest.TestCase):
    def pool(self, **over):
        roles = {"client": "10.99.1.10", "client2": "10.99.1.11", "lb": "10.99.1.20",
                 "be1": "10.99.1.31", "be2": "10.99.1.32"}
        roles.update(over)
        return [{"role": r, "privateIp": ip} for r, ip in roles.items() if ip]

    def test_accepts_the_expected_shape(self):
        self.assertIsNone(cb.check_pool(self.pool()))

    def test_refuses_missing_roles_wrong_addresses_and_gaps(self):
        self.assertIn("lb", cb.check_pool(self.pool(lb=None)))
        self.assertIn("expected", cb.check_pool(self.pool(be1="10.0.0.1")))
        self.assertIn("contiguous", cb.check_pool(self.pool(be1=None)))
        self.assertIsNotNone(cb.check_pool([{"role": "client", "privateIp": "10.99.1.10"},
                                            {"role": "lb", "privateIp": "10.99.1.20"}]))


class AwsCalls(unittest.TestCase):
    def test_refuses_odd_instance_ids(self):
        with self.assertRaises(cb.AwsError):
            cb.checked_ids(["i-0123456789abcdef0", "--dry-run"])
        self.assertEqual(cb.checked_ids(["i-0123456789abcdef0"]), ["i-0123456789abcdef0"])


class FakeHost:
    role = "be1"
    id = "i-0123456789abcdef0"
    public_ip = "192.0.2.7"


class SshArgv(unittest.TestCase):
    def setUp(self):
        self.ssh = cb.Ssh("/k.pem", "/tmp/l4mlb-test-ssh-dir", "/kh", "ssh")
        self.argv = self.ssh.argv(FakeHost(), "true")

    def opt(self, name):
        return [a.split("=", 1)[1] for a in self.argv if a.startswith(name + "=")]

    def test_host_keys_are_verified_by_instance_id(self):
        self.assertEqual(self.opt("StrictHostKeyChecking"), ["yes"])
        self.assertEqual(self.opt("HostKeyAlias"), [FakeHost.id])
        self.assertEqual(self.opt("UserKnownHostsFile"), ["/kh"])

    def test_ignores_user_config_and_forwards_nothing(self):
        self.assertEqual(self.argv[1:3], ["-F", "/dev/null"])
        self.assertEqual(self.opt("ForwardAgent"), ["no"])
        self.assertEqual(self.opt("BatchMode"), ["yes"])
        self.assertEqual(self.opt("IdentitiesOnly"), ["yes"])

    def test_secrets_are_not_in_the_ssh_environment(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "shh"
        self.addCleanup(os.environ.pop, "AWS_SECRET_ACCESS_KEY")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", self.ssh._env())


class RemoteCommands(unittest.TestCase):
    def test_numbers_cannot_smuggle_shell(self):
        cfg = {"capacity": 8, "serviceMs": 25.0, "serviceCv": 0.6, "queueLimit": 64, "errorRate": 0.0}
        args = cb.backend_args(1, cfg, cb.LB_IP, 7)
        cmd = shlex.join(args)
        self.assertEqual(shlex.split(cmd), args)
        for ch in ";|&`$<>":
            self.assertNotIn(ch, cmd)

    def test_hostnet_commands_are_fixed_strings(self):
        prep = cb.HostPrep(None)

        class H:
            role = "client"
        self.assertTrue(prep._netsetup(H).endswith("client.sh"))
        H.role = "client2"
        self.assertTrue(prep._netsetup(H).endswith("client.sh"))
        H.role = "lb"
        self.assertTrue(prep._netsetup(H).endswith("lb.sh"))
        H.role = "be3"
        self.assertTrue(prep._netsetup(H).endswith(f"backend.sh {cb.LB_IP} {cb.VIP}"))


class AgentCommands(unittest.TestCase):
    def test_valid_commands(self):
        self.assertEqual(lb_agent.admin_line({"op": "admin", "verb": "DRAIN", "id": 2}, 5), "DRAIN 2\n")
        self.assertEqual(lb_agent.admin_line({"op": "admin", "verb": "UNDRAIN", "id": 0}, 5), "UNDRAIN 0\n")

    def test_everything_else_is_ignored(self):
        for bad in ({"op": "admin", "verb": "DRAIN", "id": 5}, {"op": "admin", "verb": "DRAIN", "id": -1},
                    {"op": "admin", "verb": "DRAIN", "id": True}, {"op": "admin", "verb": "DRAIN", "id": "1"},
                    {"op": "admin", "verb": "EXEC", "id": 1}, {"op": "x"}, [], "DRAIN 1"):
            self.assertIsNone(lb_agent.admin_line(bad, 5), json.dumps(bad))

    def test_backend_spec_must_carry_a_mac(self):
        self.assertTrue(lb_agent.BACKEND_RE.match("10.99.1.31:9000@0a:00:00:00:00:01"))
        for bad in ("10.99.1.31:9000", "10.99.1.31:9000@0a:00", "x;y:9000@0a:00:00:00:00:01"):
            self.assertFalse(lb_agent.BACKEND_RE.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
