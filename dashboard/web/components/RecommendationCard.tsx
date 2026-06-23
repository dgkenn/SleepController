import { Recommendation } from '@/lib/api';
import ConfidenceMeter from './ConfidenceMeter';

interface RecommendationCardProps {
  recommendation: Recommendation;
}

const actionColors: Record<string, string> = {
  cool: 'text-cool',
  warm: 'text-warm',
  hold: 'text-gray-300',
  wake: 'text-success',
  default: 'text-brand',
};

export default function RecommendationCard({ recommendation }: RecommendationCardProps) {
  const { action, reason, confidence } = recommendation;
  const actionColor =
    actionColors[action?.toLowerCase()] ?? actionColors.default;

  return (
    <div className="bg-surface-card rounded-2xl p-4 space-y-3 border border-surface-border">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Recommendation</p>
          <p className={`text-lg font-bold ${actionColor}`}>
            {action ?? 'Hold'}
          </p>
        </div>
        <div className="shrink-0 w-24">
          <ConfidenceMeter value={confidence} size="sm" />
        </div>
      </div>
      {reason && (
        <p className="text-sm text-gray-400 leading-relaxed">{reason}</p>
      )}
      {recommendation.low_confidence && (
        <p className="text-xs text-warning bg-warning/10 rounded-lg px-2 py-1">
          Low confidence — limited data available
        </p>
      )}
    </div>
  );
}
