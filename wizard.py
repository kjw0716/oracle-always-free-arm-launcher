"""Terminal configuration and controls; no local web service or cloud writes."""
from __future__ import annotations

import configparser
import hashlib
import os
from pathlib import Path
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import oci

import grab

AUTH_FIELDS = ("user", "tenancy", "fingerprint", "key_file", "region")


def prompt(label, default="", required=True):
    while True:
        value = input(f"{label}" + (f" [{default}]" if default != "" else "") + ": ").strip()
        value = value if value else str(default)
        if value or not required:
            return value
        print("此项不能为空。")


def number(label, default, minimum, maximum):
    while True:
        value = prompt(label, default)
        try:
            grab.minutes_to_seconds(value, label, minimum, maximum)
            if minimum == 0 and 0 < float(value) < 1:
                raise grab.StopError("请输入 0（不限）或至少 1 分钟。")
            return value
        except grab.StopError as exc:
            print(exc)


def path_value(value, base):
    path = Path(os.path.expandvars(value.strip().strip('"').strip("'"))).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def parse_preview(text):
    if "PRIVATE KEY" in text:
        raise grab.StopError("此处只粘贴 Configuration file preview，不能粘贴私钥内容。")
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    if not text.lstrip().startswith("["):
        text = "[DEFAULT]\n" + text
    cfg.read_string(text)
    section = cfg["oracle"] if "oracle" in cfg else cfg["DEFAULT"]
    return {k: section[k].strip() for k in AUTH_FIELDS if k in section}


def key_fingerprint(path):
    password = os.environ.get("OCI_KEY_PASSPHRASE")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password.encode() if password else None)
    except (ValueError, TypeError) as exc:
        raise grab.StopError("无法读取 API 私钥。加密私钥请先设置 OCI_KEY_PASSPHRASE 环境变量。") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise grab.StopError("OCI API 需要至少 2048 位 RSA PEM 私钥，请勿选择 SSH 密钥。")
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return ":".join(f"{x:02x}" for x in hashlib.md5(der).digest())


def choose(label, values, display, current=""):
    if not values:
        raise grab.StopError(f"没有可选的{label}。请先在 Oracle 控制台准备资源。")
    default = 1
    print(f"\n{label}：")
    for index, value in enumerate(values, 1):
        print(f"  {index}. {display(value)}")
        if getattr(value, "id", getattr(value, "name", "")) == current:
            default = index
    while True:
        answer = prompt("输入编号", default)
        if answer.isdigit() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        print("请输入列表中的编号。")


def discover(auth, current):
    signed_auth = dict(auth)
    if os.environ.get("OCI_KEY_PASSPHRASE"):
        signed_auth["pass_phrase"] = os.environ["OCI_KEY_PASSPHRASE"]
    opts = dict(retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 45))
    identity = oci.identity.IdentityClient(signed_auth, **opts)
    network = oci.core.VirtualNetworkClient(signed_auth, **opts)
    root = auth["tenancy"]
    tenancy = identity.get_tenancy(root).data
    home = next((r.name for r in identity.list_regions().data if r.key == tenancy.home_region_key), None)
    if auth["region"] != home:
        raise grab.StopError(f"请把区域改为此租户的主区域 {home}。")
    ad = choose("可用区", grab.Oracle.all(identity.list_availability_domains, root),
                lambda x: x.name, current.get("availability_domain", ""))
    vcns = {v.id: v.display_name for v in grab.Oracle.all(network.list_vcns, root)}
    subnets = [s for s in grab.Oracle.all(network.list_subnets, root)
               if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic
               and not s.prohibit_internet_ingress and (not s.availability_domain or s.availability_domain == ad.name)]
    subnet = choose("现有公网子网（根 compartment）", subnets,
                    lambda s: f"{s.display_name} | {vcns.get(s.vcn_id, s.vcn_id)} | {s.cidr_block} | {s.id[-8:]}",
                    current.get("subnet_id", ""))
    return ad.name, subnet.id


def save_config(path, cfg):
    """Validate before atomic replacement. Never write private key data or passwords."""
    path = Path(path)
    if any(k not in AUTH_FIELDS for k in cfg["oracle"]):
        raise grab.StopError("配置中包含不支持的认证字段，拒绝保存。")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            cfg.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        settings = grab.load_settings(temp_path)
        state = grab.read_json(path.parent / "runtime" / "state.json")
        if state.get("fingerprint") not in (None, settings.fingerprint):
            raise grab.StopError("已有抢机状态与新规格不一致。请核实云端实例和未完成请求；原配置保持不变。")
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def configure(config_path):
    path = Path(config_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime = path.parent / "runtime"
    runtime.mkdir(exist_ok=True)
    with grab.single_process(runtime / "grab.lock"):
        cfg = configparser.ConfigParser(interpolation=None)
        source = path if path.exists() else Path(__file__).with_name("config.example.ini")
        cfg.read(source, encoding="utf-8-sig")
        for name in ("oracle", "instance", "retry"):
            if name not in cfg:
                cfg[name] = {}
        auth = dict(cfg["oracle"])
        print("\n配置向导（回车保留默认值，Ctrl+C 取消）")
        print("1. 粘贴 Oracle 配置预览  2. 导入 OCI 配置文件  3. 手动填写/修改现有配置")
        mode = prompt("选择", "3" if path.exists() else "1")
        if mode == "1":
            print("粘贴 Configuration file preview；最后另起一行输入英文句点 . 并回车。")
            lines = []
            while True:
                line = input()
                if line.strip() == ".":
                    break
                lines.append(line)
            auth.update(parse_preview("\n".join(lines)))
        elif mode == "2":
            source_path = path_value(prompt("OCI 配置文件", str(Path.home() / ".oci/config")), Path.cwd())
            profile = prompt("Profile", "DEFAULT")
            try:
                imported = oci.config.from_file(str(source_path), profile)
            except Exception as exc:
                raise grab.StopError(f"导入失败：{type(exc).__name__}，请检查文件和 Profile。") from exc
            auth.update({k: str(imported[k]) for k in AUTH_FIELDS if imported.get(k)})
            if auth.get("key_file"):
                auth["key_file"] = str(path_value(auth["key_file"], source_path.parent))
        elif mode != "3":
            raise grab.StopError("请选择 1、2 或 3。")
        for key, label in (("user", "User OCID"), ("tenancy", "Tenancy OCID"), ("region", "主区域 Region")):
            auth[key] = prompt(label, auth.get(key, ""))
        raw_key = auth.get("key_file", "")
        if "<" in raw_key:
            raw_key = ""
        private = path_value(prompt("API 私钥 .pem 路径", raw_key), path.parent)
        if not private.is_file():
            raise grab.StopError("API 私钥文件不存在，原配置保持不变。")
        fingerprint = key_fingerprint(private)
        if auth.get("fingerprint") and auth["fingerprint"].lower() != fingerprint:
            raise grab.StopError("该私钥与配置预览的指纹不匹配，请选择对应私钥。")
        auth.update(key_file=str(private), fingerprint=fingerprint)
        cfg["oracle"] = {k: auth[k] for k in AUTH_FIELDS}
        print("API 私钥已读取，指纹匹配。")
        inst = cfg["instance"]
        inst["ssh_public_key_file"] = str(path_value(prompt("SSH 公钥 .pub 路径", inst.get("ssh_public_key_file", "")), path.parent))
        inst["name"] = prompt("实例名称", inst.get("name") or "oci-a1")
        print("\n网络：1. 自动读取并按编号选择  2. 手动填写")
        if prompt("选择", "1") == "1":
            print("正在读取可用区和子网……")
            try:
                ad, subnet = discover(dict(cfg["oracle"]), inst)
            except grab.StopError:
                raise
            except Exception as exc:
                raise grab.StopError(f"读取失败：{getattr(exc, 'code', type(exc).__name__)}。请检查 API 密钥是否注册及用户权限。") from exc
            inst["availability_domain"], inst["subnet_id"] = ad, subnet
        else:
            inst["availability_domain"] = prompt("可用区（留空使用首个）", inst.get("availability_domain", ""), required=False)
            inst["subnet_id"] = prompt("现有公网 Subnet OCID", inst.get("subnet_id", ""))
        print("\n规格：1. 2 OCPU / 12GB  2. 1 OCPU / 6GB")
        choice = prompt("选择", "2" if inst.get("ocpus") == "1" else "1")
        if choice not in ("1", "2"):
            raise grab.StopError("规格请选择 1 或 2。")
        inst["ocpus"], inst["memory_gb"] = ("2", "12") if choice == "1" else ("1", "6")
        inst["boot_volume_gb"] = "50"
        retry = cfg["retry"]
        old_interval = retry.get("interval_minutes", str(float(retry.get("interval_seconds", "120")) / 60))
        retry["interval_minutes"] = number("每隔多少分钟重试", old_interval, 1, 60)
        retry.pop("interval_seconds", None)
        retry["max_minutes"] = number("本次最多运行多少分钟（0=不限）", retry.get("max_minutes", "0"), 0, 525600)
        retry["max_backoff_seconds"] = str(max(900, grab.minutes_to_seconds(retry["interval_minutes"], "重试间隔")))
        retry.setdefault("jitter_seconds", "30")
        retry.setdefault("check_capacity_first", "false")
        save_config(path, cfg)
        print(f"\n配置已保存：{path}\n下一步选择“检查配置”；保存配置不会创建服务器。")
    return 0


def edit_timing(config_path):
    path = Path(config_path).resolve()
    if not path.is_file():
        raise grab.StopError("请先完成配置向导。")
    runtime = path.parent / "runtime"
    runtime.mkdir(exist_ok=True)
    with grab.single_process(runtime / "grab.lock"):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(path, encoding="utf-8-sig")
        retry = cfg["retry"]
        default = retry.get("interval_minutes", str(float(retry.get("interval_seconds", "120")) / 60))
        retry["interval_minutes"] = number("每隔多少分钟重试", default, 1, 60)
        retry.pop("interval_seconds", None)
        retry["max_minutes"] = number("本次最多运行多少分钟（0=不限）", retry.get("max_minutes", "0"), 0, 525600)
        retry["max_backoff_seconds"] = str(max(900, grab.minutes_to_seconds(retry["interval_minutes"], "重试间隔")))
        save_config(path, cfg)
        print("重试时间已保存，下次启动生效。")


def menu(config_path):
    while True:
        print("\nOracle Always Free ARM Launcher\n1. 配置向导\n2. 检查配置（只读）\n3. 启动/继续抢机\n4. 查看状态\n5. 请求停止\n6. 修改重试时间\n0. 退出")
        choice = prompt("选择", "1" if not Path(config_path).exists() else "4")
        if choice == "0":
            return 0
        if choice == "6":
            try:
                edit_timing(config_path)
            except (grab.StopError, ValueError, OSError, configparser.Error) as exc:
                print(exc)
            except KeyboardInterrupt:
                print("已取消。")
            continue
        options = {"1": "--setup", "2": "--check", "3": "--resume", "4": "--status", "5": "--stop"}
        if choice not in options:
            print("请输入菜单中的编号。")
            continue
        if choice == "3":
            print("开始运行；此窗口需保持打开。Ctrl+C 停止并返回菜单。")
        grab.main(["--config", str(config_path), options[choice]])
