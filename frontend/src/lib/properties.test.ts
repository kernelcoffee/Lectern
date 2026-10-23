import { describe, expect, it } from "vitest";
import type { PropertiesResponse, PropertyDefinition } from "../api/servers";
import { propertyRows } from "./properties";

function def(managed = false): PropertyDefinition {
  return { type: "string", values: null, min: null, max: null, description: "", default: null, managed };
}

describe("propertyRows", () => {
  it("lists known keys first, then file-only keys, each alphabetical", () => {
    const data: PropertiesResponse = {
      properties: { "zeta-mod": "1", motd: "hi", "alpha-mod": "2" },
      definitions: { motd: def(), difficulty: def() },
    };
    expect(propertyRows(data)).toEqual(["difficulty", "motd", "alpha-mod", "zeta-mod"]);
  });

  it("hides managed keys absent from the file", () => {
    const data: PropertiesResponse = {
      properties: { motd: "hi" },
      definitions: { motd: def(), "management-server-enabled": def(true) },
    };
    expect(propertyRows(data)).toEqual(["motd"]);
  });

  it("shows managed keys once Lectern has written them", () => {
    const data: PropertiesResponse = {
      properties: { "management-server-port": "25585", "management-server-enabled": "true" },
      definitions: {
        "management-server-enabled": def(true),
        "management-server-port": def(true),
        "management-server-secret": def(true),
      },
    };
    expect(propertyRows(data)).toEqual(["management-server-enabled", "management-server-port"]);
  });
});
