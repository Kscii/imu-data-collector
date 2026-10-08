export type ActivityRule = {code: string; name_zh: string; is_fall: boolean;
  definition: string; includes: string; excludes: string; boundaries: string; example: string};
export type ActivityRules = {id: string; version: string; primary: ActivityRule[]; principles: string[];
  training_profile: {id: string; version: string; codes: string[]; mapping: Record<string, string>; unknown_policy: string};
  rolling: {status: string; definition: string}; [key: string]: unknown};
