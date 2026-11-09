"""Read-only live HK quote validation. No rankings, files, AI calls or publication."""
import json
from picker import EM_HOSTS_HK, EM_FS_HK, fetch_market


def main():
    frame = fetch_market('港股', EM_HOSTS_HK, EM_FS_HK, 'HK', None, None)
    print(json.dumps(frame.attrs['quality'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
