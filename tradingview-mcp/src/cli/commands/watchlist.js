import { register } from '../router.js';
import * as core from '../../core/watchlist.js';

register('watchlist', {
  description: 'Watchlist tools (get, add, remove, select-list)',
  subcommands: new Map([
    ['get', {
      description: 'Get watchlist symbols',
      handler: () => core.get(),
    }],
    ['add', {
      description: 'Add a symbol to the watchlist',
      options: {
        list: { type: 'string', description: 'Named watchlist to switch to first' },
      },
      handler: (opts, positionals) => {
        if (!positionals[0]) throw new Error('Symbol required. Usage: tv watchlist add AAPL [--list "Bot Watchlist"]');
        return core.add({ symbol: positionals[0], list: opts.list });
      },
    }],
    ['remove', {
      description: 'Remove a symbol from the currently active watchlist',
      handler: (opts, positionals) => {
        if (!positionals[0]) throw new Error('Symbol required. Usage: tv watchlist remove AAPL');
        return core.remove({ symbol: positionals[0] });
      },
    }],
    ['select-list', {
      description: 'Switch the active watchlist to a named list',
      handler: (opts, positionals) => {
        if (!positionals[0]) throw new Error('Name required. Usage: tv watchlist select-list "Bot Watchlist"');
        return core.selectList({ name: positionals[0] });
      },
    }],
  ]),
});
