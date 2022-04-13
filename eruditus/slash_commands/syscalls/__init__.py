import os
from collections import OrderedDict

import discord
from discord import app_commands
from discord.app_commands import Choice

from enum import Enum
from typing import Union, List


class CPUArchitecture(Enum):
    x86 = 1
    x64 = 2
    arm = 3
    armthumb = 4


class SyscallTable:
    """Represents an architecture's syscall table."""

    def __init__(self, architecture: str) -> None:
        self.syscalls = {}
        self.parse_table(architecture)

    def parse_table(self, filename: str) -> None:
        lines = [line.split("\t") for line in open(filename).readlines()]

        for line in lines[1:]:
            syscall = OrderedDict()

            for idx, identifier in enumerate(lines[0]):
                identifier = identifier.strip()
                if identifier == "Definition":
                    syscall[identifier] = line[idx].split(":")[0]
                    continue

                syscall[identifier] = line[idx]

            self.syscalls[line[1]] = syscall

    def get_syscall_by_name(self, name: str) -> Union[OrderedDict, None]:
        return self.syscalls.get(name) if name in self.syscalls else None


class Syscalls(app_commands.Command):
    architectures = {
        arch.value: SyscallTable(
            f"{os.path.dirname(os.path.abspath(__file__))}/tables/{arch.name}"
        )
        for arch in CPUArchitecture
    }

    def __init__(self):
        super().__init__(
            name="syscalls",
            description="Show information about a syscall from a specific architecture",
            callback=self.callback,
        )

        @self.autocomplete("syscall")
        async def autocomplete_func(
            interaction: discord.Interaction, current: str
        ) -> List[Choice[str]]:
            """Autocomplete syscall name.

            Args:
                interaction: The interaction that triggered this command.
                current: The syscall name typed so far.

            Returns:
                A list of suggestions.
            """
            suggestions = []
            for syscall_name in Syscalls.architectures[
                interaction.namespace.arch
            ].syscalls:
                if current.lower() in syscall_name:
                    suggestions.append(Choice(name=syscall_name, value=syscall_name))
                if len(suggestions) == 25:
                    break
            return suggestions

    async def callback(
        self, interaction: discord.Interaction, arch: CPUArchitecture, syscall: str
    ) -> None:
        """Show information for a syscall from a specific architecture.

        Args:
            inetraction: The interaction that triggered this command.
            arch: The CPU architecture.
            syscall: The syscall name.
        """
        syscall_info = Syscalls.architectures[arch.value].get_syscall_by_name(syscall)

        if syscall_info is None:
            await interaction.response.send_message(
                f"No such syscall: {syscall}", ephemeral=True
            )
        else:
            formatted_info = "\n".join(
                f"{key + ':':15} {syscall_info[key]}" for key in syscall_info
            )
            await interaction.response.send_message(f"```yaml\n{formatted_info}\n```")
