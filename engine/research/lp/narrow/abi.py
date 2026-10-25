from __future__ import annotations

SEL_SLOT0 = "0x3850c7bd"
SEL_LIQUIDITY = "0x1a686502"
SEL_TICK_SPACING = "0xd0c93a7c"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"
SEL_TICK_BITMAP = "0x5339c296"
SEL_TICKS = "0xf30dba93"
SEL_MULTICALL_TIMESTAMP = "0x0f28c97d"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MASK256 = (1 << 256) - 1


def encode_int(value: int) -> str:
    return f"{value & MASK256:064x}"


def words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def to_signed(value: int, bits: int = 256) -> int:
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def address_of(word: int) -> str:
    return f"0x{word & ((1 << 160) - 1):040x}"
