import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
  // Each test installs its own bridge and media query; none may leak into the next one. Main-process
  // tests run in the node environment, where there is no window at all.
  if (typeof window !== 'undefined') {
    Reflect.deleteProperty(window, 'clipforge');
    Reflect.deleteProperty(window, 'matchMedia');
  }
});
