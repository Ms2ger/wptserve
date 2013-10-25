#!/usr/bin/env python
import argparse
import server
import os

def abs_path(path):
    return os.path.abspath(path)

def parse_args():
    parser = argparse.ArgumentParser(description="HTTP server designed for extreme flexibility for use in testing situations.")
    parser.add_argument("document_root", action="store", type=abs_path,
                        help="Root directory to serve files from")
    parser.add_argument("--port", "-p", dest="port", action="store",
                        type=int, default=8000,
                        help="Port number to run server on")
    parser.add_argument("--host", "-H", dest="host", action="store",
                        type=str, default="127.0.0.1",
                        help="Host to run server on")
    return parser.parse_args()


def main():
    args = parse_args()
    httpd = server.WebTestHttpd(router, host=args.host, port=args.port,
                                use_ssl=False, certificate=None,
                                doc_root=args.document_root)
    httpd.start()

if __name__ == "__main__":
    main()