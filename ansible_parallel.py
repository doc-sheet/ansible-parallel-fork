import argparse
import asyncio
import os
import subprocess
import sys
from collections import defaultdict
from shutil import get_terminal_size
from time import perf_counter
from typing import List, Tuple


COMMON_ANSIBLE_OPTIONS = (
    "-l",
    "--limit",
    "-e",
    "--extra-vars",
    "-t",
    "--tags",
    "--skip-tags",
    "-f",
    "--forks",
    "-u",
    "--user",
)


class CommonArgs(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.common_options += (str(option_string), *values)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playbook", nargs="+")
    common_group = parser.add_argument_group("Bypass options")
    common_group.add_argument(
        *COMMON_ANSIBLE_OPTIONS,
        nargs=1,
        action=CommonArgs,
        default=[],
        dest="common_options",
        help=argparse.SUPPRESS,
    )
    return parser.parse_known_args()


def prepare_chunk(playbook, chunk: str) -> Tuple[str, str, str]:
    """Parse a chunk of ansible-playbook output.

    Given an ansible-playbook output chunk, like:

    TASK [staging : Install sudo] ********************************************
    ok: [staging1.eeple.net]

    return a tree-tuple:
    - Chunk type:
    - playbook name
    - the actual chunk.

    """
    lines = chunk.strip().split("\n")
    if len(lines) >= 2:
        if "PLAY RECAP" in chunk:
            return ("RECAP", playbook, chunk)
        if "ok:" in lines[1]:
            return ("OK", playbook, chunk)
        if "changed:" in lines[1]:
            return ("CHANGED", playbook, chunk)
        if "failed:" in lines[1] or "fatal:" in lines[1]:
            return ("ERROR", playbook, chunk)
        if "unreachable:" in lines[1]:
            return ("UNREACHABLE", playbook, chunk)
    if chunk.startswith("TASK"):
        return ("TASK", playbook, chunk)
    if "ERROR!" in chunk:
        return ("ERROR", playbook, chunk)
    return ("MSG", playbook, chunk)


async def run_playbook(playbook, args, results: asyncio.Queue):
    await results.put(("START", playbook, ""))
    process = await asyncio.create_subprocess_exec(
        "ansible-playbook",
        playbook,
        *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "ANSIBLE_FORCE_COLOR": "1"},
    )
    assert process.stdout

    task: List[str] = []
    while True:
        line = (await process.stdout.readline()).decode()
        if not line:
            break
        if line == "\n":
            chunk = "".join(task) + line
            await results.put(prepare_chunk(playbook, chunk))
            task = []
        else:
            task.append(line)
    if task:
        chunk = "".join(task)
        await results.put(prepare_chunk(playbook, chunk))

    await process.wait()
    if process.returncode:
        await results.put(
            ("DONE", playbook, f"Exited with error code: {process.returncode}")
        )
    else:
        await results.put(("DONE", playbook, "Done."))
    return process.returncode


FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

DISABLE_CURSOR = "\033[?25l"
ENABLE_CURSOR = "\033[?25h"


def truncate(string, max_width):
    if len(string) <= max_width:
        return string
    return string[: max_width - 1] + "…"


async def show_progression(results: asyncio.Queue, playbooks: List[str], stream):
    recaps = defaultdict(list)
    starts = {}
    ends = {}
    currently_running = []
    frameno = 0
    stream.write(DISABLE_CURSOR)
    longest_name = max(len(playbook) for playbook in playbooks)
    for playbook in playbooks:
        stream.write(playbook + ": \n")
    columns, _ = get_terminal_size()
    try:
        while True:
            result = await results.get()
            if not result:
                break
            frameno += 1
            msgtype, playbook, msg = result
            position = playbooks.index(playbook)
            diff = len(playbooks) - position
            stream.write(f"\033[{diff}A")
            stream.write(
                f"\033[{longest_name + 2}C"
            )  # Move right after the playbook name and :.
            if msgtype == "START":
                starts[playbook] = perf_counter()
                currently_running.append(playbook)
                stream.write("\033[0K")  # EL – Erase In Line with parameter 0.
                stream.write("\033[m")  # Select Graphic Rendition: Attributes off.
                stream.write("Started")
            if msgtype == "DONE":
                currently_running.remove(playbook)
                ends[playbook] = perf_counter()
                stream.write("\033[0K")  # EL – Erase In Line with parameter 0.
                stream.write("\033[m")  # Select Graphic Rendition: Attributes off.
                stream.write(msg)
            if msgtype == "RECAP":
                recaps[playbook].append(msg)
            if msgtype == "TASK":
                stream.write("\033[0K")  # EL – Erase In Line with parameter 0.
                stream.write("\033[m")  # Select Graphic Rendition: Attributes off.
                stream.write(
                    truncate(msg.split("\n")[0], max_width=columns - longest_name - 4)
                )
            if msgtype == "ERROR":
                recaps[playbook].append(msg)
            stream.write(f"\033[{diff}B")
            stream.write(f"\033[{columns + 1}D")
            stream.flush()
    finally:
        stream.write(ENABLE_CURSOR)
        stream.flush()

    stream.write("\n")
    for playbook, recap in recaps.items():
        stream.write(
            f"# Playbook {playbook}, ran in {ends[playbook] - starts[playbook]:.0f}s\n"
        )
        for chunk in recap:
            for line in chunk.split("\n"):
                if "PLAY RECAP" in line:
                    continue
                stream.write(line)
                stream.write("\n")
    stream.flush()


async def playbook_wrapper(semaphore, playbook, remaining_args, results_queue):
    async with semaphore:
        return await run_playbook(playbook, remaining_args, results_queue)


def check_playbooks(playbooks: List[str]) -> bool:
    # Verify all playbook files can be found

    fail = False
    for playbook in playbooks:
        if not os.path.isfile(playbook):
            print("Could not find playbook:", playbook, file=sys.stderr)
            fail = True
    return fail


async def amain():
    args, remaining_args = parse_args()
    if os.environ.get("ANSIBLE_PARALLEL_VERIFY_PLAYBOOKS") and check_playbooks(
        args.playbook
    ):
        return 1

    results_queue = asyncio.Queue()
    printer_task = asyncio.create_task(
        show_progression(results_queue, args.playbook, sys.stderr)
    )
    semaphore = asyncio.Semaphore(
        int(os.environ.get("ANSIBLE_PARALLEL_MAX_PLAYBOOKS", 5))
    )

    playbook_args = (*args.common_options, *remaining_args)
    playbook_tasks = [
        asyncio.create_task(
            playbook_wrapper(semaphore, playbook, playbook_args, results_queue)
        )
        for playbook in args.playbook
    ]

    results = await asyncio.gather(*playbook_tasks)

    await results_queue.put(None)
    await printer_task
    return sum(results)


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
