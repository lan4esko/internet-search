"""Build tool argv from validated profile data; subprocesses never use a shell."""


def build_masscan_command(profile: dict, networks: list[str], output_path: str, status_option: str | None = None) -> list[str]:
    config = profile["masscan"]
    command = ["masscan", *networks, "--ports", config["ports"], "--rate", str(config["rate"]),
               "--output-format", config.get("output_format", "json"), "--output-filename", output_path]
    for field, flag in (("wait", "--wait"), ("retries", "--retries"), ("adapter", "--adapter"),
                        ("source_port", "--source-port"), ("seed", "--seed")):
        if config.get(field) is not None:
            command.extend([flag, str(config[field])])
    if config.get("banners"):
        command.append("--banners")
    if status_option:
        command.append(f"{status_option}=true")
    return command


def build_httpx_command(profile: dict, input_path: str, output_path: str) -> list[str]:
    config = profile["httpx"]
    command = ["httpx", "-list", input_path, "-output", output_path, "-json", "-silent", "-no-color",
               "-disable-update-check", "-no-fallback-scheme", "-probe", "-status-code", "-title", "-web-server",
               "-content-type", "-content-length", "-response-time", "-include-response",
               "-response-size-to-read", str(config["response_size_limit"]),
               "-response-size-to-save", str(config["response_size_limit"])]
    for field, flag in (("threads", "-threads"), ("rate_limit", "-rate-limit"), ("timeout", "-timeout"),
                        ("retries", "-retries"), ("method", "-x")):
        if config.get(field) is not None:
            command.extend([flag, str(config[field])])
    if config.get("paths"):
        command.extend(["-path", ",".join(config["paths"])])
    if config.get("tech_detect"):
        command.append("-tech-detect")
    if config.get("favicon"):
        command.append("-favicon")
    if config.get("follow_redirects"):
        command.append("-follow-host-redirects")
        if config.get("max_redirects") is not None:
            command.extend(["-max-redirects", str(config["max_redirects"])])
    for header in config.get("headers", []):
        command.extend(["-H", header])
    return command
