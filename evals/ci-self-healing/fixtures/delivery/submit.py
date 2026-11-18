"""Send one handoff to the host's local test receiver."""
import argparse
import json
from pathlib import Path
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--to", required=True)
parser.add_argument("--material", required=True)
args = parser.parse_args()
connection = json.loads(Path(__file__).with_name("connection.json").read_bytes())
body = json.dumps({"to": args.to, "material": args.material}).encode()
request = urllib.request.Request(connection["url"] + "/handoff", data=body,
                                 headers={"Authorization": "Bearer " + connection["token"]})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
    print(response.read().decode())
