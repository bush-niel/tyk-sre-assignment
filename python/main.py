import sys
import signal
import argparse

from kubernetes import client, config

from app import app


def handle_shutdown(signum, frame):
    """Stop the HTTP server when the process receives SIGTERM or SIGINT."""
    print(f"Received signal {signum}, shutting down")
    app.stop_server()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tyk SRE Assignment",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-k", "--kubeconfig",
        type=str,
        default="",
        help="path to kubeconfig, leave empty for in-cluster"
    )
    parser.add_argument(
        "-a", "--address",
        type=str,
        default=":8080",
        help="HTTP server listen address"
    )
    args = parser.parse_args()

    if args.kubeconfig != "":
        config.load_kube_config(config_file=args.kubeconfig)
    else:
        config.load_incluster_config()

    api_client = client.ApiClient()

    try:
        version = app.get_kubernetes_version(api_client)
    except Exception as exc:
        print(exc)
        sys.exit(1)

    print(f"Connected to Kubernetes {version}")

    # Keep the shared clients inside the app module so the handler can use them.
    app.configure_kubernetes(api_client)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        app.start_server(args.address)
    except KeyboardInterrupt:
        print("Server terminated")
