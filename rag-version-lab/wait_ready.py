"""Wait for phase 2 API and the ingestion worker, without exposing response data."""
import json
import time
import urllib.request


def main(timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8089/api/health', timeout=5) as response:
                state = json.load(response)
            if state.get('api') == 'ok' and state.get('worker_alive') is True:
                return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit('第二阶段 API/Worker 未就绪，请查看日志')


if __name__ == '__main__':
    main()
