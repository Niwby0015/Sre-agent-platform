"""Read-only Windows host resource inspection tools."""
from __future__ import annotations

import json
import platform
import subprocess

from langchain.tools import tool


_HOST_METRICS_SCRIPT = r"""
$cpu = Get-CimInstance Win32_Processor |
  Measure-Object -Property LoadPercentage -Average
$os = Get-CimInstance Win32_OperatingSystem
[PSCustomObject]@{
  cpu_percent = [math]::Round([double]$cpu.Average, 1)
  logical_processors = [int]($env:NUMBER_OF_PROCESSORS)
  memory_total_mb = [math]::Round([double]$os.TotalVisibleMemorySize / 1KB, 0)
  memory_available_mb = [math]::Round([double]$os.FreePhysicalMemory / 1KB, 0)
} | ConvertTo-Json -Compress
"""


def _nvidia_gpu_metrics() -> list[str]:
    """Return one concise line per NVIDIA GPU, or a reason it is unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,utilization.memory,"
                "memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return ["GPU: NVIDIA GPU not detected (nvidia-smi is unavailable)."]
    except subprocess.TimeoutExpired:
        return ["GPU: nvidia-smi timed out."]

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return [f"GPU: nvidia-smi failed: {detail[0] if detail else 'unknown error'}"]

    lines = []
    for index, row in enumerate(result.stdout.splitlines()):
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 6:
            continue
        name, gpu_util, memory_util, memory_used, memory_total, temperature = fields
        # ``utilization.memory`` is memory-controller activity, not VRAM
        # capacity used. Calculate the latter explicitly so the output cannot
        # be misread (e.g. 1004/6144 MiB is ~16.3%, not 1%).
        try:
            capacity_percent = float(memory_used) / float(memory_total) * 100
            capacity_text = f"{capacity_percent:.1f}% capacity used"
        except (TypeError, ValueError, ZeroDivisionError):
            capacity_text = "capacity used percentage unavailable"
        lines.append(
            f"GPU {index}: {name}; GPU core={gpu_util}%; "
            f"VRAM={memory_used}/{memory_total} MiB ({capacity_text}); "
            f"memory controller={memory_util}%; "
            f"temperature={temperature} C"
        )
    return lines or ["GPU: nvidia-smi returned no GPU data."]


@tool
def get_windows_host_metrics() -> str:
    """Show CPU, memory, and NVIDIA GPU utilization for the local Windows host.

    This reads the computer running the SRE agent, not a Kubernetes node. It is
    read-only. GPU details are returned only when the NVIDIA driver utility
    ``nvidia-smi`` is installed and can access a GPU.
    """
    if platform.system() != "Windows":
        return "ERROR: get_windows_host_metrics is available only when the agent runs on Windows."

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _HOST_METRICS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: Windows performance query timed out."

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return f"ERROR: Windows performance query failed: {detail[0] if detail else 'unknown error'}"
    # PowerShell can emit non-terminating CIM errors while still returning 0;
    # do not present the resulting zero values as genuine utilization readings.
    if result.stderr.strip():
        detail = result.stderr.strip().splitlines()
        return f"ERROR: Windows performance query failed: {detail[0]}"

    try:
        data = json.loads(result.stdout)
        total = float(data["memory_total_mb"])
        available = float(data["memory_available_mb"])
        used = max(total - available, 0)
        memory_percent = (used / total * 100) if total else 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "ERROR: Windows performance query returned an unexpected response."

    lines = [
        "LOCAL WINDOWS HOST METRICS",
        f"CPU: {data['cpu_percent']}% across {data['logical_processors']} logical processors",
        f"Memory: {used:.0f}/{total:.0f} MiB ({memory_percent:.1f}% used; {available:.0f} MiB available)",
        *_nvidia_gpu_metrics(),
    ]
    return "\n".join(lines)
