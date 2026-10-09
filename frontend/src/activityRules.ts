export type ActivityRule = {code: string; name_zh: string; is_fall: boolean;
  definition: string; includes: string; excludes: string; boundaries: string; example: string};
export type ActivityRules = {id: string; version: string; primary: ActivityRule[]; principles: string[];
  segmentation?: {version: string; minimum_interval_ns: number; hard_seconds: number};
  training_profile: {id: string; version: string; codes: string[]; mapping: Record<string, string>; unknown_policy: string};
  rolling: {status: string; definition: string}; [key: string]: unknown};
