from typing import Tuple

from solders.keypair import Keypair


def generate_keypair() -> Tuple[str, str]:
    """Generate a new Solana keypair. Returns (public_key, private_key_base58)."""
    kp = Keypair()
    public_key = str(kp.pubkey())
    private_key = str(kp)
    return public_key, private_key


def keypair_from_private_key(private_key_base58: str) -> Keypair:
    return Keypair.from_base58_string(private_key_base58)
