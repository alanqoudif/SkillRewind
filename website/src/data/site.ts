// Central config module. Literals used across the site (URLs, doc paths, status text)
// live here so copy stays consistent with the actual SkillRewind repository.

export const siteTitle = 'SkillRewind';
export const siteTagline = 'Traceable, revocable, and rebuildable persistent learning for AI agents.';
export const siteDescription =
  'SkillRewind is an open-source research and infrastructure project that makes persistent AI-agent artifacts ' +
  '(skills, memories, procedures, prompt patches) attributable, revocable, selectively rebuildable, and ' +
  'verifiable within a declared evidence boundary — as a platform-agnostic service any agent platform can ' +
  'integrate with over a documented HTTP API.';

export const siteUrl = import.meta.env.PUBLIC_SITE_URL || 'https://skillrewind.dev';

export const githubUrl = 'https://github.com/alanqoudif/SkillRewind';
export const githubOrgRepo = 'alanqoudif/SkillRewind';

export const status = {
  version: 'v0.3.0a1',
  label: 'Alpha · Research & Integration Preview',
  license: 'Apache-2.0',
  pythonRequires: '>=3.11 (3.11, 3.12)',
};

export const docs = {
  integrationContract: `${githubUrl}/blob/main/docs/integration-contract-v1.md`,
  apiStability: `${githubUrl}/blob/main/docs/api-stability-v1.md`,
  openapi: `${githubUrl}/blob/main/docs/openapi-v1.json`,
  eventContract: `${githubUrl}/blob/main/docs/event-contract-v1.md`,
  threatModel: `${githubUrl}/blob/main/docs/threat-model.md`,
  releaseReadiness: `${githubUrl}/blob/main/docs/release-readiness-v0.3.md`,
  security: `${githubUrl}/blob/main/SECURITY.md`,
  status: `${githubUrl}/blob/main/STATUS.md`,
  license: `${githubUrl}/blob/main/LICENSE`,
  citation: `${githubUrl}/blob/main/CITATION.cff`,
  rfc: `${githubUrl}/blob/main/rfc/0001-hidden-lineage-revocation.md`,
  attestationSchema: `${githubUrl}/blob/main/spec/revocation-attestation.schema.json`,
  researchProposal: `${githubUrl}/blob/main/paper/SkillRewind_Research_Proposal_v0.2.pdf`,
};

export const nav = [
  { label: 'Problem', href: '#problem' },
  { label: 'How it works', href: '#lifecycle' },
  { label: 'Specification', href: '#specification' },
  { label: 'Research', href: '#research' },
  { label: 'Architecture', href: '#architecture' },
  { label: 'Docs', href: '#developer' },
];
