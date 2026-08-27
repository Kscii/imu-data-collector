export type AnnotationShortcutAction =
  | { kind: "step"; delta: -5 | -1 | 1 | 5 }
  | { kind: "mark"; target: "start" | "end" | "impact" }
  | { kind: "select"; target: "fall" | "non_fall" | "exclude" }
  | { kind: "jump_selected_end" }
  | { kind: "save" };

export type AnnotationShortcutInput = {
  code: string;
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  repeat: boolean;
  isComposing: boolean;
  textEntryFocused: boolean;
};

export function resolveAnnotationShortcut(
  input: AnnotationShortcutInput,
  canEdit: boolean,
  fallSelected: boolean,
): AnnotationShortcutAction | null {
  if (input.isComposing || input.textEntryFocused) return null;
  if (
    !input.ctrlKey
    && !input.metaKey
    && !input.altKey
    && (input.code === "Comma" || input.code === "Period")
  ) {
    const direction = input.code === "Comma" ? -1 : 1;
    return { kind: "step", delta: (direction * (input.shiftKey ? 5 : 1)) as -5 | -1 | 1 | 5 };
  }
  if (
    (input.ctrlKey || input.metaKey)
    && !input.altKey
    && input.key.toLowerCase() === "s"
  ) {
    return canEdit ? { kind: "save" } : null;
  }
  if (input.ctrlKey || input.metaKey || input.altKey || input.repeat) return null;
  const key = input.key.toLowerCase();
  if (key === "e") return { kind: "jump_selected_end" };
  if (!canEdit) return null;
  if (key === "i") return { kind: "mark", target: "start" };
  if (key === "o") return { kind: "mark", target: "end" };
  if (key === "2" && fallSelected) return { kind: "mark", target: "impact" };
  if (key === "f") return { kind: "select", target: "fall" };
  if (key === "n") return { kind: "select", target: "non_fall" };
  if (key === "x") return { kind: "select", target: "exclude" };
  return null;
}
