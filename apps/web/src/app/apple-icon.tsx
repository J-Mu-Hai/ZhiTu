import { ImageResponse } from 'next/og';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

export const runtime = 'nodejs';
export const size = { width: 180, height: 180 };
export const contentType = 'image/png';

/** Generate the home-screen icon from the same SVG used throughout the site. */
export default async function AppleIcon() {
  const svg = await readFile(join(process.cwd(), 'src/app/icon.svg'));
  return new ImageResponse(
    // ImageResponse renders its own image primitives, not next/image.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={`data:image/svg+xml;base64,${svg.toString('base64')}`} width={180} height={180} alt="" />,
    size,
  );
}
