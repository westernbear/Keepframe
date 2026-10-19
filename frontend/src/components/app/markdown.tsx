import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'

const PLUGINS = [remarkGfm]

/** Model text as GitHub-flavoured markdown. Raw HTML stays text (no rehype-raw) and images are dropped: the model
 *  reads text out of frames, and an image URL in its reply would make the browser fetch whatever it was told to. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn('md', className)}>
      <ReactMarkdown remarkPlugins={PLUGINS} disallowedElements={['img']}
        components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" /> }}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
