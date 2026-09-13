/**
 * Where the server is, and the token to talk to it.
 *
 * A phone cannot assume `localhost`, so the server address is a setting rather
 * than a constant. The token is stored in the platform keychain via
 * expo-secure-store on iOS/Android; on web, SecureStore does not exist and it
 * falls back to AsyncStorage (localStorage), which is the honest limit of what
 * a browser can offer.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import Constants from 'expo-constants';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

const URL_KEY = 'careeros.baseUrl';
const TOKEN_KEY = 'careeros.token';

export type Settings = { baseUrl: string; token: string | null };

/**
 * Best default per platform:
 * - web: same origin, because the FastAPI app serves this build itself.
 * - native: the dev machine's LAN address, inferred from the Expo dev server
 *   when available, since that is where the API almost certainly runs.
 */
export function defaultBaseUrl(): string {
  if (Platform.OS === 'web') {
    if (typeof window !== 'undefined' && window.location?.origin) {
      return window.location.origin;
    }
    return 'http://localhost:8000';
  }
  const hostUri = Constants.expoConfig?.hostUri ?? (Constants as any).expoGoConfig?.debuggerHost;
  const host = typeof hostUri === 'string' ? hostUri.split(':')[0] : null;
  return host ? `http://${host}:8000` : 'http://localhost:8000';
}

const secureAvailable = Platform.OS !== 'web';

async function readToken(): Promise<string | null> {
  try {
    if (secureAvailable) return await SecureStore.getItemAsync(TOKEN_KEY);
    return await AsyncStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

async function writeToken(token: string | null): Promise<void> {
  try {
    if (secureAvailable) {
      if (token) await SecureStore.setItemAsync(TOKEN_KEY, token);
      else await SecureStore.deleteItemAsync(TOKEN_KEY);
      return;
    }
    if (token) await AsyncStorage.setItem(TOKEN_KEY, token);
    else await AsyncStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable (private mode, blocked site data): run in-memory */
  }
}

export async function loadSettings(): Promise<Settings> {
  let baseUrl = defaultBaseUrl();
  try {
    baseUrl = (await AsyncStorage.getItem(URL_KEY)) || baseUrl;
  } catch {
    /* fall through to the default */
  }
  return { baseUrl, token: await readToken() };
}

export async function saveSettings(settings: Settings): Promise<void> {
  try {
    await AsyncStorage.setItem(URL_KEY, settings.baseUrl);
  } catch {
    /* ignore */
  }
  await writeToken(settings.token);
}

export const tokenStorageNote = secureAvailable
  ? 'Stored in the device keychain.'
  : 'Stored in this browser only. Use a device build for keychain storage.';
