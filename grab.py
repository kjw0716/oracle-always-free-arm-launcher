#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Derived from Yohann0617/oci-helper, Copyright 2024 Yohann0617.
# Modified: Python port of instance creation/retry logic; see NOTICE.txt.
"""One-instance OCI A1 launcher. No AI, web server, database or telemetry."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import oci

LOG = logging.getLogger("oracle-always-free-arm-launcher")
SHAPE = "VM.Standard.A1.Flex"
LIVE = lambda x: x.lifecycle_state not in ("TERMINATED", "DELETED")


class StopError(Exception):
    """A condition which cannot be fixed by retrying."""


class AlreadyRunning(StopError):
    pass


@dataclass
class Settings:
    auth: dict
    name: str
    ad: str
    vcn_name: str
    subnet_name: str
    subnet_id: str
    image_id: str
    public_key: str
    ocpus: float
    memory: float
    disk: int
    interval: int
    jitter: int
    max_backoff: int
    capacity_first: bool
    max_minutes: float = 0

    @property
    def fingerprint(self):
        # A pending request must never be replayed with different parameters.
        data = {k: v for k, v in vars(self).items()
                if k not in ("auth", "interval", "jitter", "max_backoff", "capacity_first", "max_minutes")}
        data["tenancy"] = self.auth["tenancy"]
        data["region"] = self.auth["region"]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def minutes_to_seconds(value, label, minimum=1, maximum=60):
    try:
        minutes = float(value)
    except (ValueError, TypeError) as exc:
        raise StopError(f"{label}必须是数字。") from exc
    if not math.isfinite(minutes) or not minimum <= minutes <= maximum:
        raise StopError(f"{label}须为 {minimum}–{maximum} 分钟。")
    return round(minutes * 60)


def load_settings(path, interval_minutes=None, max_minutes=None):
    path = Path(path).resolve()
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(path, encoding="utf-8-sig"):
        raise StopError(f"找不到配置文件：{path}")
    for section in ("oracle", "instance", "retry"):
        if section not in cfg:
            raise StopError(f"配置缺少 [{section}]")
    auth = {k: v for k, v in cfg["oracle"].items()
            if k in ("user", "tenancy", "fingerprint", "key_file", "region")}
    if os.environ.get("OCI_KEY_PASSPHRASE"):
        auth["pass_phrase"] = os.environ["OCI_KEY_PASSPHRASE"]
    missing = [k for k in ("user", "tenancy", "fingerprint", "key_file", "region")
               if not auth.get(k, "").strip() or auth[k].startswith("YOUR_")]
    if missing:
        raise StopError("请在 config.ini 的 [oracle] 填写：" + ", ".join(missing))

    def local_file(value):
        p = Path(os.path.expandvars(value)).expanduser()
        return p if p.is_absolute() else path.parent / p

    auth["key_file"] = str(local_file(auth["key_file"]).resolve())
    if not Path(auth["key_file"]).is_file():
        raise StopError("OCI API 私钥文件不存在（不是 SSH 私钥）。")
    oci.config.validate_config(auth)
    inst, retry = cfg["instance"], cfg["retry"]
    interval = (minutes_to_seconds(interval_minutes, "重试间隔") if interval_minutes is not None
                else minutes_to_seconds(retry["interval_minutes"], "重试间隔") if "interval_minutes" in retry
                else retry.getint("interval_seconds", 120))
    duration = max_minutes if max_minutes is not None else retry.get("max_minutes", "0")
    if 0 < float(duration) < 1:
        raise StopError("运行时长为 0（不限）或至少 1 分钟。")
    duration_seconds = minutes_to_seconds(duration, "运行时长", 0, 525600)
    key_file = local_file(inst.get("ssh_public_key_file", ""))
    if not key_file.is_file():
        raise StopError("SSH 公钥文件不存在；请填写 ssh_public_key_file。")
    public_key = key_file.read_text(encoding="utf-8").strip()
    if len(public_key.splitlines()) != 1 or not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise StopError("SSH 文件必须是单行 .pub 公钥，不能是私钥。")
    s = Settings(auth=auth, name=inst.get("name", "oci-a1"),
                 ad=inst.get("availability_domain", ""),
                 vcn_name=inst.get("vcn_name", ""),
                 subnet_name=inst.get("subnet_name", ""),
                 subnet_id=inst.get("subnet_id", ""), image_id=inst.get("image_id", ""),
                 public_key=public_key, ocpus=inst.getfloat("ocpus", 2),
                 memory=inst.getfloat("memory_gb", 12), disk=inst.getint("boot_volume_gb", 50),
                 interval=interval, jitter=retry.getint("jitter_seconds", 30),
                 max_backoff=max(interval, retry.getint("max_backoff_seconds", 900)),
                 capacity_first=retry.getboolean("check_capacity_first", False),
                 max_minutes=duration_seconds / 60)
    if not s.subnet_id and not (s.vcn_name and s.subnet_name):
        raise StopError("请填写 subnet_id，或同时填写 vcn_name 和 subnet_name。")
    if not s.name.strip() or len(s.name) > 200:
        raise StopError("实例名称不能为空或超过 200 字符。")
    if not (math.isfinite(s.ocpus) and math.isfinite(s.memory)
            and s.ocpus in (1, 2) and s.ocpus <= s.memory <= 12):
        raise StopError("配置限制：1–2 OCPU，内存至少等于 OCPU 数且不超过 12GB。")
    if not (47 <= s.disk <= 50):
        raise StopError("此精简版启动盘限定 47–50GB。")
    if not (60 <= s.interval <= 3600 and 0 <= s.jitter <= 300
            and s.interval <= s.max_backoff <= 7200):
        raise StopError("重试间隔须为 60–3600 秒，抖动 0–300 秒，最大退避不小于间隔且不超过 7200 秒。")
    return s


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextmanager
def single_process(path):
    # Kernel-held lock survives neither a crash nor process exit; no stale PID problem.
    f = Path(path).open("a+b")
    try:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning("已有进程正在使用此配置目录，请勿重复启动或修改。") from exc
        yield
    finally:
        f.close()


def classify_error(exc):
    if isinstance(exc, oci.exceptions.ServiceError):
        code = str(exc.code or "").lower()
        message = str(exc.message or "").lower()
        if code in ("limitexceeded", "servicelimitexceeded", "quotaexceeded"):
            return "fatal"
        if exc.status in (401, 403, 404):
            return "fatal"
        if exc.status == 429 or code == "toomanyrequests":
            return "throttle"
        if ("out of capacity" in message or "out of host capacity" in message
                or code == "outofhostcapacity"):
            return "capacity"
        if exc.status in (408, 409, 500, 502, 503, 504):
            return "transient"
        return "fatal"
    if isinstance(exc, (oci.exceptions.RequestException, TimeoutError, ConnectionError)):
        return "transient"
    return "fatal"


class Oracle:
    def __init__(self, settings):
        self.s = settings
        opts = dict(retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 45))
        self.compute = oci.core.ComputeClient(settings.auth, **opts)
        self.network = oci.core.VirtualNetworkClient(settings.auth, **opts)
        self.identity = oci.identity.IdentityClient(settings.auth, **opts)
        self.storage = oci.core.BlockstorageClient(settings.auth, **opts)
        self.root = settings.auth["tenancy"]

    @staticmethod
    def all(method, *args, **kwargs):
        return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data

    def preflight(self):
        tenancy = self.identity.get_tenancy(self.root).data
        regions = self.identity.list_regions().data
        home = next((r.name for r in regions if r.key == tenancy.home_region_key), None)
        if home != self.s.auth["region"]:
            raise StopError(f"配置区域不是此租户主区域 {home}；停止创建，请核对 region。")
        self.ads = self.all(self.identity.list_availability_domains, self.root)
        ad_names = [ad.name for ad in self.ads]
        if not ad_names:
            raise StopError("账户没有可用区。")
        ad = self.s.ad or ad_names[0]
        if ad not in ad_names:
            raise StopError("availability_domain 不属于当前租户/区域。")
        compartments = self.all(self.identity.list_compartments, self.root,
                                compartment_id_in_subtree=True, access_level="ANY")
        self.compartments = [self.root] + [c.id for c in compartments if c.lifecycle_state == "ACTIVE"]
        subnet_id = self.s.subnet_id
        if not subnet_id:
            vcns = self.all(self.network.list_vcns, self.root)
            vcns = [v for v in vcns if v.display_name == self.s.vcn_name and v.lifecycle_state == "AVAILABLE"]
            if len(vcns) != 1:
                raise StopError("找不到唯一的现有 VCN；请核对 vcn_name 或填写 subnet_id。")
            subnets = self.all(self.network.list_subnets, self.root, vcn_id=vcns[0].id)
            subnets = [s for s in subnets if s.display_name == self.s.subnet_name and s.lifecycle_state == "AVAILABLE"]
            if len(subnets) != 1:
                raise StopError("找不到唯一的现有子网；请核对 subnet_name 或填写 subnet_id。")
            subnet_id = subnets[0].id
        subnet = self.network.get_subnet(subnet_id).data
        if (subnet.lifecycle_state != "AVAILABLE" or subnet.prohibit_public_ip_on_vnic
                or subnet.prohibit_internet_ingress or subnet.compartment_id != self.root
                or (subnet.availability_domain and subnet.availability_domain != ad)):
            raise StopError("子网必须是根 compartment 中兼容可用区的现有公网子网。")
        images = self.all(self.compute.list_images, self.root, shape=SHAPE,
                          operating_system="Oracle Linux", operating_system_version="9",
                          sort_by="TIMECREATED", sort_order="DESC")
        images = [i for i in images if i.lifecycle_state == "AVAILABLE" and not i.compartment_id]
        if self.s.image_id:
            images = [i for i in images if i.id == self.s.image_id]
        if not images:
            raise StopError("未找到匹配 A1 的 Oracle Linux 9 官方平台镜像。")
        image = images[0]
        shapes = self.all(self.compute.list_shapes, self.root, availability_domain=ad, image_id=image.id)
        if not any(s.shape == SHAPE for s in shapes):
            raise StopError("所选镜像/可用区不支持 A1。")
        self.request = dict(
            availability_domain=ad, compartment_id=self.root, display_name=self.s.name,
            shape=SHAPE, shape_config=dict(ocpus=self.s.ocpus, memory_in_gbs=self.s.memory),
            source_details=dict(source_type="image", image_id=image.id,
                                boot_volume_size_in_gbs=self.s.disk, boot_volume_vpus_per_gb=10),
            create_vnic_details=dict(subnet_id=subnet_id, assign_public_ip=True),
            metadata={"ssh_authorized_keys": self.s.public_key},
            is_pv_encryption_in_transit_enabled=True)
        LOG.info("配置通过：%s | A1 | %s OCPU / %sGB | 启动盘 %sGB", self.s.auth["region"], self.s.ocpus, self.s.memory, self.s.disk)

    def instances(self):
        return [i for compartment in self.compartments
                for i in self.all(self.compute.list_instances, compartment) if LIVE(i)]

    def check_usage(self, instances):
        # This tool creates at most ONE A1 and conservatively stops if any A1 exists.
        if any(i.shape == SHAPE for i in instances):
            raise StopError("租户已有其他 A1 实例；停止以避免重复创建或超过免费额度。")
        used = 0
        for compartment in self.compartments:
            volumes = list(self.all(self.storage.list_volumes, compartment_id=compartment))
            for ad in self.ads:
                volumes += self.all(self.storage.list_boot_volumes, availability_domain=ad.name,
                                    compartment_id=compartment)
            for volume in volumes:
                if LIVE(volume):
                    if volume.size_in_gbs is None:
                        raise StopError("无法确认现有磁盘用量，停止自动创建。")
                    used += volume.size_in_gbs
        if used + self.s.disk > 200:
            raise StopError(f"磁盘总量 {used}+{self.s.disk}GB 超过 200GB，停止自动创建。")

    def capacity(self):
        m = oci.core.models
        report = self.compute.create_compute_capacity_report(m.CreateComputeCapacityReportDetails(
            compartment_id=self.root, availability_domain=self.request["availability_domain"],
            shape_availabilities=[m.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE, instance_shape_config=m.CapacityReportInstanceShapeConfig(
                    ocpus=self.s.ocpus, memory_in_gbs=self.s.memory))])).data
        statuses = [x.availability_status for x in report.shape_availabilities]
        if "AVAILABLE" in statuses:
            return True
        if statuses and all(x == "OUT_OF_HOST_CAPACITY" for x in statuses):
            return False
        raise StopError("容量报告不支持此规格或状态未知；可将 check_capacity_first 改为 false 使用原项目的直接创建重试。")

    def launch(self, request, token):
        m = oci.core.models
        body = dict(request)
        body["shape_config"] = m.LaunchInstanceShapeConfigDetails(**body["shape_config"])
        body["source_details"] = m.InstanceSourceViaImageDetails(**body["source_details"])
        body["create_vnic_details"] = m.CreateVnicDetails(**body["create_vnic_details"])
        return self.compute.launch_instance(m.LaunchInstanceDetails(**body), opc_retry_token=token,
                                           retry_strategy=oci.retry.NoneRetryStrategy()).data

    def instance(self, instance_id):
        return self.compute.get_instance(instance_id).data

    def public_ip(self, instance):
        attachments = self.all(self.compute.list_vnic_attachments,
                               compartment_id=instance.compartment_id, instance_id=instance.id)
        for a in attachments:
            if a.vnic_id and a.lifecycle_state == "ATTACHED":
                vnic = self.network.get_vnic(a.vnic_id).data
                if vnic.is_primary:
                    return vnic.public_ip
        return None


class Runner:
    def __init__(self, settings, backend, directory, clock=time.time):
        self.s, self.api, self.directory, self.clock = settings, backend, Path(directory), clock
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state.get("fingerprint") not in (None, settings.fingerprint):
            raise StopError("配置与已有状态不一致；请先核实已有实例和未完成请求，不要直接删除状态文件。")
        self.state["fingerprint"] = settings.fingerprint

    def save(self):
        self.state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        write_json(self.path, self.state)

    def adopt(self, instance):
        cfg = instance.shape_config
        if (instance.shape != SHAPE or cfg is None or cfg.ocpus != self.s.ocpus
                or cfg.memory_in_gbs != self.s.memory or instance.display_name != self.s.name):
            raise StopError("同名实例规格不符；停止创建，请检查控制台。")
        self.state["instance_id"] = instance.id
        self.state.pop("pending", None)
        self.save()  # Record acceptance BEFORE any waiting or IP lookup.
        if instance.lifecycle_state not in ("RUNNING", "PROVISIONING", "STARTING"):
            raise StopError(f"目标实例状态为 {instance.lifecycle_state}；请检查控制台，不会自动补建。")
        if instance.lifecycle_state != "RUNNING":
            LOG.info("已占到实例，当前状态 %s；仅查询状态，不重复创建。", instance.lifecycle_state)
            return "provisioning"
        ip = self.api.public_ip(instance)
        if not ip:
            LOG.info("实例已运行，等待公网 IP；不重复创建。")
            return "provisioning"
        self.state.update(status="complete", public_ip=ip)
        self.save()
        LOG.info("创建成功：%s | IP %s | SSH 用户 opc。程序停止。", self.s.name, ip)
        return "complete"

    def step(self):
        if self.state.get("status") == "complete":
            LOG.info("已成功完成，停止。IP：%s", self.state.get("public_ip", ""))
            return "complete"
        if self.state.get("instance_id"):
            return self.adopt(self.api.instance(self.state["instance_id"]))
        instances = self.api.instances()  # Failure here must never be treated as an empty list.
        matches = [i for i in instances if i.display_name == self.s.name]
        if len(matches) > 1:
            raise StopError("发现多个同名实例，停止并等待人工核对。")
        if matches:
            return self.adopt(matches[0])
        pending = self.state.get("pending")
        if pending and self.clock() - pending["created_at"] >= 23 * 3600:
            raise StopError("未确认的创建请求已接近幂等令牌有效期；请在控制台核实，避免重复创建。")
        self.api.check_usage(instances)
        if not pending:
            if self.s.capacity_first and not self.api.capacity():
                self.state["status"] = "out_of_capacity"
                self.save()
                return "capacity"
            pending = {"token": uuid.uuid4().hex, "created_at": self.clock(), "request": self.api.request}
            self.state["pending"] = pending
            self.state["status"] = "request_pending"
            self.save()  # A durable retry token before issuing a non-idempotent operation.
        self.state["attempts"] = self.state.get("attempts", 0) + 1
        self.save()
        try:
            instance = self.api.launch(pending["request"], pending["token"])
        except Exception as exc:
            kind = classify_error(exc)
            if kind == "capacity":
                self.state.pop("pending", None)  # Server explicitly rejected allocation.
                self.state["status"] = "out_of_capacity"
                self.save()
            # A timeout/500/429 retains the SAME token and exact request on disk.
            raise
        return self.adopt(instance)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def is_running(runtime):
    if not (runtime / "grab.lock").exists():
        return False
    try:
        with single_process(runtime / "grab.lock"):
            return False
    except AlreadyRunning:
        return True


def show_status(runtime):
    active = is_running(runtime)
    record = read_json(runtime / "run.json")
    state = read_json(runtime / "state.json")
    print("进程：" + ("运行中（或配置正在编辑）" if active else "未运行"))
    labels = {"waiting": "等待重试", "checking": "检查配置", "requesting": "请求中",
              "complete": "已完成", "stopped": "已停止", "time_limit": "达到运行时长",
              "needs_attention": "需要处理", "interrupted": "已中断", "checked": "检查通过"}
    print("最近活动：" + labels.get(record.get("status"), record.get("status", "尚未运行")))
    print(f"累计创建尝试：{state.get('attempts', 0)}")
    if record.get("updated_at"):
        print("更新时间（UTC）：" + record["updated_at"])
    if active and record.get("next_attempt_at"):
        print("下次尝试（UTC）：" + record["next_attempt_at"])
    if (runtime / "STOP").exists():
        print("已请求停止；启动时加 --resume 可恢复。")
    if state.get("public_ip"):
        print("公网 IP：" + state["public_ip"])
    attention = read_json(runtime / "needs-attention.json")
    if attention:
        print("需要处理：" + attention.get("message", "请查看日志"))
    if not active and record.get("status") in ("waiting", "requesting", "checking"):
        print("上次进程未正常退出；再次启动会保留请求状态并核对云端结果。")
    return 0


def run_session(args, runtime):
    record = {"pid": os.getpid(), "status": "checking"}

    def activity(status, **extra):
        record.pop("next_attempt_at", None)
        record.update(status=status, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **extra)
        write_json(runtime / "run.json", record)

    try:
        if args.resume:
            (runtime / "STOP").unlink(missing_ok=True)
        if (runtime / "STOP").exists():
            LOG.info("已请求停止。使用 --resume 或菜单中的启动/继续来恢复。")
            activity("stopped")
            return 0
        activity("checking")
        settings = load_settings(args.config, args.interval_minutes, args.max_minutes)
        backend = Oracle(settings)
        runner = Runner(settings, backend, runtime)
        if runner.state.get("status") == "complete":
            runner.step()
            activity("complete")
            return 0
        delay = settings.interval
        finish = time.monotonic() + settings.max_minutes * 60 if settings.max_minutes else None
        LOG.info("重试间隔 %g 分钟（另加 0–%s 秒抖动）；运行时长：%s。",
                 settings.interval / 60, settings.jitter,
                 f"{settings.max_minutes:g} 分钟" if finish else "不限")
        while True:
            if (runtime / "STOP").exists():
                activity("stopped")
                LOG.info("收到停止请求，已保存状态。")
                return 0
            if finish is not None and time.monotonic() >= finish:
                activity("time_limit")
                LOG.info("达到本次运行时长，已保存状态；下次启动可继续。")
                return 0
            try:
                if not hasattr(backend, "request"):
                    backend.preflight()
                    (runtime / "needs-attention.json").unlink(missing_ok=True)
                if args.check:
                    rows = backend.instances()
                    if any(i.display_name == settings.name for i in rows):
                        LOG.info("发现同名实例；正式运行将先核对，不直接创建。")
                    else:
                        backend.check_usage(rows)
                    LOG.info("检查完成，未创建任何资源。")
                    activity("checked")
                    return 0
                if (runtime / "STOP").exists() or (finish is not None and time.monotonic() >= finish):
                    continue
                activity("requesting")
                outcome = runner.step()
                if outcome == "complete":
                    activity("complete")
                    return 0
                delay = settings.interval
                if outcome == "capacity":
                    LOG.info("容量报告显示缺货，等待下一轮。")
            except StopError:
                raise
            except Exception as exc:
                kind = classify_error(exc)
                if kind == "fatal":
                    code = getattr(exc, "code", type(exc).__name__)
                    raise StopError(f"停止：{code}。请检查凭据、权限、配额或配置。详见 Oracle 控制台。") from exc
                if args.check:
                    raise StopError("本次检查遇到临时 API 错误，请稍后重新 --check。") from exc
                delay = settings.interval if kind == "capacity" else min(max(delay * 2, 300), settings.max_backoff)
                LOG.info({"capacity": "A1 当前缺货。", "throttle": "请求被限流，退避等待。",
                          "transient": "网络/API 暂时异常；先核实结果，再使用原令牌重试。"}[kind])
            if args.once:
                activity("stopped")
                return 3
            wait = delay + random.randint(0, settings.jitter)
            if finish is not None:
                wait = min(wait, max(0, finish - time.monotonic()))
            wake = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))
            activity("waiting", next_attempt_at=wake)
            LOG.info("约 %.2f 分钟后继续；--stop 可停止。", wait / 60)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if (runtime / "STOP").exists():
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        activity("interrupted")
        LOG.info("已停止；下次启动将核对并恢复已保存的请求。")
        return 130
    except Exception as exc:
        # Never dump SDK objects, credentials or request bodies into a public-facing error.
        known = isinstance(exc, (StopError, ValueError, OSError, configparser.Error, oci.exceptions.InvalidConfig))
        message = str(exc) if known else f"停止：{type(exc).__name__}，请检查配置和依赖。"
        LOG.error(message)
        write_json(runtime / "needs-attention.json", {"status": "needs_attention", "message": message})
        activity("needs_attention")
        return 2


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="精简 OCI A1 抢机：缺货重试，成功一台即停止，不调用 AI。")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.ini")))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读检查账户、网络、镜像和资源用量")
    mode.add_argument("--once", action="store_true", help="最多执行一轮（可能创建）")
    mode.add_argument("--setup", action="store_true", help="交互式配置向导")
    mode.add_argument("--menu", action="store_true", help="打开操作菜单")
    mode.add_argument("--status", action="store_true", help="查看本地进程、尝试次数与结果")
    mode.add_argument("--stop", action="store_true", help="请求停止，不删除已保存的状态")
    parser.add_argument("--resume", action="store_true", help="清除停止标记后运行")
    parser.add_argument("--interval-minutes", type=float, help="本次重试间隔，1–60 分钟")
    parser.add_argument("--max-minutes", type=float, help="本次最长运行分钟数，0 表示不限")
    args = parser.parse_args(argv)
    runtime = Path(args.config).resolve().parent / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        if args.menu or args.setup:
            import wizard
            return wizard.menu(args.config) if args.menu else wizard.configure(args.config)
        if args.status:
            return show_status(runtime)
        if args.stop:
            (runtime / "STOP").touch()
            print("已请求停止。正在执行的 API 请求结束后停止；下次启动用 --resume。")
            return 0
        with single_process(runtime / "grab.lock"):
            LOG.setLevel(logging.INFO)
            for handler in LOG.handlers:
                handler.close()
            LOG.handlers.clear()
            for handler in (logging.StreamHandler(), RotatingFileHandler(runtime / "grab.log", maxBytes=1024*1024, backupCount=2, encoding="utf-8")):
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
                LOG.addHandler(handler)
            return run_session(args, runtime)
    except KeyboardInterrupt:
        print("已取消。")
        return 130
    except (StopError, ValueError, OSError, configparser.Error, EOFError, oci.exceptions.InvalidConfig) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for handler in LOG.handlers:
            handler.close()
        LOG.handlers.clear()


if __name__ == "__main__":
    # The wizard imports this module; keep one set of exception classes/loggers.
    sys.modules.setdefault("grab", sys.modules[__name__])
    sys.exit(main())
