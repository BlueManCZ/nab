import logging
import sys


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from nab.application import NabApplication

    app = NabApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
