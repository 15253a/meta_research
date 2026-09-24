export type RuntimeConditionsObject = Record<string, unknown>;

export type ParsedRuntimeConditions =
  | { kind: "structured"; prefix: string; suffix: string; data: RuntimeConditionsObject }
  | { kind: "text" | "invalid" };

export type RuntimeConditionDevice = RuntimeConditionsObject & {
  uuid: string;
  name?: string;
  memory_total_mib?: number;
};

const NO_SELECTED_GPU = "未配置已选 GPU；不得将机器探测到的其他显卡当作用户已选择的资源。";

export function isConditionsObject(value: unknown): value is RuntimeConditionsObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Keep the surrounding instructions verbatim. A broken outer object must never
// be replaced by a nested object that happens to parse successfully.
export function parseRuntimeConditions(text: string): ParsedRuntimeConditions {
  const start = text.indexOf("{");
  if (start < 0) return { kind: "text" };
  let depth = 0;
  let quoted = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') quoted = false;
      continue;
    }
    if (character === '"') quoted = true;
    else if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth !== 0) continue;
      try {
        const data: unknown = JSON.parse(text.slice(start, index + 1));
        if (!isConditionsObject(data)) return { kind: "invalid" };
        return { kind: "structured", prefix: text.slice(0, start), suffix: text.slice(index + 1), data };
      } catch {
        return { kind: "invalid" };
      }
    }
  }
  return { kind: "invalid" };
}

export function replaceRuntimeConditionsJson(
  parsed: Extract<ParsedRuntimeConditions, { kind: "structured" }>,
  data: RuntimeConditionsObject,
): string {
  return `${parsed.prefix}${JSON.stringify(data, null, 2)}${parsed.suffix}`;
}

export function runtimeConditionDevices(data: RuntimeConditionsObject): RuntimeConditionDevice[] | null {
  const value = data.selected_devices;
  if (value === undefined) return [];
  if (!Array.isArray(value) || !value.every(device => isConditionsObject(device)
    && typeof device.uuid === "string" && device.uuid.length > 0
    && (device.name === undefined || typeof device.name === "string")
    && (device.memory_total_mib === undefined || typeof device.memory_total_mib === "number"))) return null;
  return value;
}

export function runtimeConditionDeviceIds(data: RuntimeConditionsObject): string[] | null {
  const devices = runtimeConditionDevices(data);
  if (!devices) return null;
  const value = data.selected_device_uuids;
  if (value !== undefined && (!Array.isArray(value) || !value.every(uuid => typeof uuid === "string" && uuid.length > 0))) return null;
  const deviceIds = devices.map(device => device.uuid);
  if (new Set(deviceIds).size !== deviceIds.length || (value !== undefined && new Set(value).size !== value.length)) return null;
  if (data.selected_devices !== undefined && value !== undefined
    && (value.length !== deviceIds.length || value.some(uuid => !deviceIds.includes(uuid)))) return null;
  return value === undefined ? deviceIds : [...value];
}

export function mergeRuntimeConditionDevices(
  previous: RuntimeConditionDevice[], next: RuntimeConditionDevice[],
): RuntimeConditionDevice[] {
  const byUuid = new Map(previous.map(device => [device.uuid, device]));
  for (const device of next) byUuid.set(device.uuid, device);
  return [...byUuid.values()];
}

export function updateRuntimeConditionDevices(
  data: RuntimeConditionsObject, selectedIds: string[], availableDevices: RuntimeConditionDevice[],
): RuntimeConditionsObject {
  const byUuid = new Map<string, RuntimeConditionDevice>(availableDevices.map(({ uuid, name, memory_total_mib }) => [uuid, { uuid, name, memory_total_mib }]));
  // Current raw JSON is authoritative, including deliberately removed fields.
  for (const device of runtimeConditionDevices(data) ?? []) byUuid.set(device.uuid, device);
  const next: RuntimeConditionsObject = {
    ...data,
    selected_device_uuids: selectedIds,
    selected_devices: selectedIds.map(uuid => byUuid.get(uuid) ?? { uuid }),
  };
  if (selectedIds.length && next.gpu_configuration === NO_SELECTED_GPU) {
    delete next.gpu_configuration;
  } else if (!selectedIds.length && next.gpu_configuration === undefined) {
    next.gpu_configuration = NO_SELECTED_GPU;
  }
  return next;
}
