import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    'index',
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: ['setup', 'running'],
    },
    {
      type: 'category',
      label: 'Architecture',
      collapsed: false,
      items: ['architecture', 'strategy', 'execution'],
    },
    {
      type: 'category',
      label: 'Database',
      collapsed: false,
      items: ['database/index', 'database/schema', 'database/operations'],
    },
    {
      type: 'category',
      label: 'Reference',
      collapsed: false,
      items: ['configuration', 'api', 'dashboard'],
    },
    {
      type: 'category',
      label: 'Development',
      collapsed: false,
      items: ['testing', 'performance'],
    },
  ],
};

export default sidebars;
