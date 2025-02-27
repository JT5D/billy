import argparse
import logging
import importlib

from billy.conf import settings, base_arg_parser
from billy.utils import configure_logging
from billy.commands import BaseCommand

logger = logging.getLogger('billy')
configure_logging()

COMMAND_MODULES = (
    # lots of these commands can go away as billy matures
    'billy.commands.serve',             # useful for development
    'billy.commands.textextract',       # useful for development
    'billy.commands.download_photos',
    'billy.commands.dump',
    'billy.commands.update_external_ids',
    'billy.commands.update_leg_ids',
    'billy.commands.validate_api',
)

if settings.ENABLE_OYSTER:
    COMMAND_MODULES += ('billy.commands.oysterize',)


def import_command_module(mod):
    try:
        importlib.import_module(mod)
    except ImportError, e:
        logger.warning(
            'error "{0}" prevented loading of {1} module'.format(e, mod))


def main():
    parser = argparse.ArgumentParser(description='generic billy util',
                                     parents=[base_arg_parser])
    subparsers = parser.add_subparsers(dest='subcommand')

    # import command plugins
    for mod in COMMAND_MODULES:
        import_command_module(mod)

    # instantiate all subcommands
    subcommands = {}
    for SubcommandCls in BaseCommand.subcommands:
        subcommands[SubcommandCls.name] = SubcommandCls(subparsers)

    # parse arguments, update settings, then run the appropriate function
    args = parser.parse_args()
    settings.update(args)
    configure_logging(args.subcommand)
    subcommands[args.subcommand].handle(args)

if __name__ == '__main__':
    main()
