import { SearchX } from 'lucide-react';
import { Link } from 'react-router-dom';
import { EmptyState } from '@/components/kit/EmptyState';
import { buttonVariants } from '@/components/ui/button';

export default function NotFoundPage() {
  return (
    <main className="grid min-h-dvh place-items-center px-4">
      <div className="w-full max-w-[480px]">
        <h1 className="sr-only">Página não encontrada</h1>
        <EmptyState
          icon={SearchX}
          title="Página não encontrada"
          action={
            <Link to="/dashboard" className={buttonVariants()}>
              Ir para o Dashboard
            </Link>
          }
        >
          O endereço não existe ou foi movido.
        </EmptyState>
      </div>
    </main>
  );
}
