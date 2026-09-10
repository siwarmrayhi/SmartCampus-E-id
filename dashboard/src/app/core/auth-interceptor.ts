import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { Auth } from './auth';

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(Auth);
  const token = auth.getToken();

  if (token) {
    req = req.clone({
      setHeaders: { Authorization: `Bearer ${token}` },
    });
  }

  return next(req).pipe(
    catchError((erreur: HttpErrorResponse) => {
      // NOUVEAU — session expirée ou invalide : on déconnecte
      // automatiquement plutôt que de laisser la personne bloquée sur
      // une page qui ne répond plus.
      if (erreur.status === 401) {
        auth.logout();
      }
      return throwError(() => erreur);
    }),
  );
};