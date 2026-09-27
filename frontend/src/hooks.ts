import { useEffect, useState } from "react";
import { query } from "./api";
import type { Row } from "./api";

export function useQuery<T>(
  method: string,
  args: Row = {},
  refresh = 0,
  enabled = true,
) {
  const argsKey = JSON.stringify(args);
  const key = `${method}:${refresh}:${enabled}:${argsKey}`;
  const [state, setState] = useState<{
    key: string;
    data: T | null;
    error: unknown;
  }>({ key: "", data: null, error: null });
  useEffect(() => {
    const controller = new AbortController();
    if (enabled)
      query<T>(method, JSON.parse(argsKey), controller.signal)
        .then((data) => {
          if (!controller.signal.aborted) setState({ key, data, error: null });
        })
        .catch((error) => {
          if (!controller.signal.aborted) setState({ key, data: null, error });
        });
    return () => controller.abort();
  }, [method, argsKey, key, enabled]);
  return {
    data: enabled && state.key === key ? state.data : null,
    error: enabled && state.key === key ? state.error : null,
    loading: enabled && state.key !== key,
  };
}
