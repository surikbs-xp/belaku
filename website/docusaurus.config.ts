import type {Config} from '@docusaurus/types';
import type {Options as PresetOptions} from '@docusaurus/preset-classic';
import {themes as prismThemes} from 'prism-react-renderer';

const config: Config = {
  title: 'Belaku',
  tagline: 'Algorithmic execution for GIFT Nifty → Nifty Futures',
  favicon: 'img/favicon.svg',

  future: {
    v4: true,
  },

  url: 'https://surikbs-xp.github.io',
  baseUrl: '/belaku/',

  organizationName: 'surikbs-xp',
  projectName: 'belaku',
  trailingSlash: false,

  onBrokenLinks: 'throw',

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/surikbs-xp/belaku/tree/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies PresetOptions,
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: false,
    },
    navbar: {
      title: 'Belaku',
      logo: {
        alt: 'Belaku',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/surikbs-xp/belaku',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Setup', to: '/setup'},
            {label: 'Architecture', to: '/architecture'},
            {label: 'Database', to: '/database'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'Configuration', to: '/configuration'},
            {label: 'API', to: '/api'},
            {label: 'Performance', to: '/performance'},
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/surikbs-xp/belaku',
            },
          ],
        },
      ],
      copyright: 'Belaku — algorithmic execution for GIFT Nifty → Nifty Futures',
    },
    prism: {
      theme: prismThemes.oneDark,
      darkTheme: prismThemes.oneDark,
      additionalLanguages: ['bash', 'json', 'sql', 'python', 'yaml'],
    },
    tableOfContents: {
      minHeadingLevel: 2,
      maxHeadingLevel: 4,
    },
  } satisfies Config['themeConfig'],
};

export default config;
