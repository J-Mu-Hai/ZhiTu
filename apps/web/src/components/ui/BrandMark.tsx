import Image from 'next/image';

/** One artwork for the browser tab, navigation, auth and assistant identity. */
export function BrandMark({ size = 24 }: { size?: number }) {
  return <Image className="brand-mark" src="/icon.svg" width={size} height={size} alt="" aria-hidden="true" unoptimized />;
}
