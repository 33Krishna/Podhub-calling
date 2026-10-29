import os
import wave
import numpy as np
from dotenv import load_dotenv
from google import genai
import sanotts

load_dotenv()

def required_ascii_secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value or not value.isascii() or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be non-empty ASCII without whitespace")
    if value.endswith(",") or any(character in value for character in "‘’“”"):
        raise ValueError(f"{name} contains a trailing comma or typographical quotes")
    return value


def check_credentials() -> None:
    required_ascii_secret("GOOGLE_API_KEY")
    required_ascii_secret("VIDEOSDK_TOKEN")


def check_sanotts() -> None:
    synthesizer = sanotts.Synthesizer(voice="heart")
    result = synthesizer.synthesize("Local voice synthesis is operational and running cleanly.")
    assert result.audio is not None and result.sample_rate == 24_000

    audio_data = np.asarray(result.audio)
    if audio_data.dtype.kind == "f":
        audio_data = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16)
    else:
        audio_data = audio_data.astype(np.int16, copy=False)
    assert audio_data.size > 0

    with wave.open("test.wav", "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(result.sample_rate)
        wav_file.writeframes(audio_data.tobytes())

    with wave.open("test.wav", "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getframerate() == 24_000
        assert wav_file.getnframes() > 0


def check_gemini() -> None:
    api_key = required_ascii_secret("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents="Reply with exactly: Gemini connected successfully.",
    )
    assert response.text and response.text.strip()


def main() -> int:
    checks = (("Credentials", check_credentials), ("SanoTTS", check_sanotts), ("Gemini SDK", check_gemini))
    failed = False
    for name, check in checks:
        try:
            check()
            print(f"PASS: {name}")
        except Exception as error:
            failed = True
            print(f"FAIL: {name}: {error}")
    if not failed:
        print("Run the worker with: .venv/bin/python main.py")
        print("Configure the VideoSDK routing rule for agent_id=my_telephony_agent.")
        print("Trigger an outbound test call with the VideoSDK SIP Call API using your destination number and auth token.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())