"""Read-only quote validation. No rankings, files, AI calls or publication."""
import argparse
import json
from datetime import datetime, timezone
from picker import EM_HOSTS_HK, EM_FS_HK, EM_HOSTS_A, EM_FS_A, fetch_market


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("A", "HK"), default="HK")
    args = parser.parse_args(argv)
    label, hosts, fs = (("A股", EM_HOSTS_A, EM_FS_A) if args.market == "A"
                        else ("港股", EM_HOSTS_HK, EM_FS_HK))
    print("探针采集开始 UTC:", datetime.now(timezone.utc).isoformat(), flush=True)
    frame = fetch_market(label, hosts, fs, args.market, None, None)
    print(json.dumps(frame.attrs["quality"], ensure_ascii=False, indent=2))
    print("注意：上述时间是采集时间，不是交易所报价时间；探针不更新网站。", flush=True)


if __name__ == "__main__":
    main()
