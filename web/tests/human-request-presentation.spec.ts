import { expect, test } from "@playwright/test";
import type { HumanRequestItem } from "../src/api";
import {
  humanRequestPresentation,
  plainRequestText,
} from "../src/humanRequestPresentation";

const technicalReferences = [
  {
    name: "URLs and their query values",
    reference: "https://example.org/Target/manifest.json?Dataset=Baseline#Variant",
  },
  {
    name: "Windows paths, including quoted paths with spaces",
    reference: '"C:\\Research Target Notes\\Target\\manifest.json"',
  },
  {
    name: "POSIX paths",
    reference: "/srv/Target/Skill/manifest.json",
  },
  {
    name: "relative paths",
    reference: "../Target/Skill/manifest.json",
  },
  {
    name: "bare filenames",
    reference: "Dataset.csv、manifest.json、Skill.md",
  },
  {
    name: "inline code",
    reference: "`exec_command` 和 `apply_patch --input manifest.json`",
  },
  {
    name: "fenced code",
    reference: '```python\nTarget = "Baseline"\nexec_command("manifest.json")\n```',
  },
  {
    name: "tilde-fenced code",
    reference: '~~~python\nTarget = "Baseline"\nexec_command("manifest.json")\n~~~',
  },
];

for (const { name, reference } of technicalReferences) {
  test(`plain request text preserves ${name} while translating surrounding prose`, () => {
    expect(plainRequestText(`Target 需要参考以下内容：\n${reference}\n请按 Skill 处理。`))
      .toBe(`研究任务 需要参考以下内容：\n${reference}\n请按 操作说明 处理。`);
  });
}

test("plain request text translates system terms in ordinary prose", () => {
  expect(plainRequestText("Quest 的 Target 需按 Skill 核对冻结 manifest，并使用已接纳的 Baseline。"))
    .toBe("研究项目 的 研究任务 需按 操作说明 核对本次任务的输入文件清单，并使用已确认的 参考方法。");
});

test("offline requests can ask for expert judgment without implying a required site visit", () => {
  const request: HumanRequestItem = {
    request_ref: "expert-direction:r1",
    request_id: "expert-direction",
    revision: 1,
    issuer: "agent_runtime",
    kind: "offline_action",
    status: "open",
    obligation: "请专家判断研究方向是否需要调整。",
    business_purpose: "现有证据不足以选择下一步，需要导师提供判断与建议。",
    acceptance_conditions: ["说明建议的研究方向以及理由。"],
  };
  const presentation = humanRequestPresentation(request);
  expect(presentation.action).toBe(request.obligation);
  expect(presentation.checks).toEqual(request.acceptance_conditions);
  expect(presentation.summary).toMatch(/判断|意见|帮助|建议/);
  expect(presentation.summary).not.toContain("这项工作需要现场操作或观察");
  expect(presentation.handler).not.toBe("能在现场操作或观察的人");
});
