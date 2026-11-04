#!/usr/bin/env node
// Unit test for the elicitation path (item 3).
//
// The agent sends elicitation/create with a JSON schema that has "__other"
// properties and oneOf option lists. T3 does not understand that shape, so
// the proxy rewrites it: the other-properties are dropped, the option lists
// become enums, and the answer is mapped back when the client replies.
//
// The proxy exports its handlers when it is required as a module, so this
// drives them directly.
//
//   node test_mcode_acp_elicitation.mjs [proxy]
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const require = createRequire(import.meta.url);
const { interceptOutgoing, interceptIncoming, patchReleaseChunks } = require(PROXY);

let failures = 0;
function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

// The agent's elicitation request.
const request = {
  jsonrpc: '2.0',
  id: 42,
  method: 'elicitation/create',
  params: {
    sessionId: 'sess-1',
    message: 'Pick a deployment target',
    requestedSchema: {
      type: 'object',
      properties: {
        target: {
          type: 'string',
          title: 'Target',
          oneOf: [
            { const: 'staging', title: 'Staging' },
            { const: 'prod', title: 'Production' },
          ],
        },
        notes__other: { type: 'string', title: 'Other notes' },
      },
      required: ['target', 'notes__other'],
    },
  },
};

const out = JSON.parse(interceptOutgoing(JSON.stringify(request)));
const schema = out.params.requestedSchema;
check('the request is still an elicitation', out.method === 'elicitation/create' && out.id === 42, JSON.stringify(out).slice(0, 80));
check('the option list became an enum', Array.isArray(schema.properties.target.enum)
  && schema.properties.target.enum.join(',') === 'Staging,Production', JSON.stringify(schema.properties.target.enum));
check('the other-property was dropped', !('notes__other' in schema.properties), JSON.stringify(Object.keys(schema.properties)));
check('the other-property left the required list', !schema.required.includes('notes__other'), JSON.stringify(schema.required));

// The client's answer: the user picked an option and typed a custom note.
// The schema no longer shows the __other property, so the client answers with
// the base key and the proxy maps the value back.
const answer = {
  jsonrpc: '2.0',
  id: 42,
  result: {
    action: 'accept',
    content: { target: 'prod', notes: 'use the EU region' },
  },
};
const answered = JSON.parse(interceptIncoming(JSON.stringify(answer)));
check('the chosen option survived', answered.result.content.target === 'prod', JSON.stringify(answered.result.content));
check('the custom answer was mapped back', answered.result.content.notes__other === 'use the EU region', JSON.stringify(answered.result.content));

// The runtime chunk patch: the questionnaire handling.
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mcode-chunks-'));
const chunks = path.join(dir, 'lib/node_modules/@minimax-ai/code/chunks');
fs.mkdirSync(chunks, { recursive: true });
const chunk = path.join(chunks, 'run-acp-command-test.js');
fs.writeFileSync(chunk, [
  'if(e.type==="questionnaire.dismiss"||e.type==="questionnaire.superseded")return ho("questionnaire",e.requestId,e.sessionId);',
  'if(t.type==="questionnaire.dismiss"||t.type==="questionnaire.superseded"){this.settleQuestionnaire(t.requestId,!1);return}',
  'settleQuestionnaire(t,r){let o=this.questionTurnByRequestId.get(t);o&&(this.questionnaireOutcomes.set(o,r),this.wake())}',
].join('\n'));
patchReleaseChunks(path.join(dir, 'launcher'));
const patched = fs.readFileSync(chunk, 'utf8');
check('the questionnaire dismissal patch was applied', !patched.includes('questionnaire.dismiss"||'), '');
check('the superseded case is still handled', patched.includes('questionnaire.superseded'), '');
check('the settle patch was applied', patched.includes('if(o){if(this.questionnaireOutcomes.get(o)===!0&&r===!1)return;'), '');
fs.rmSync(dir, { recursive: true, force: true });

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
