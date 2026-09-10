package com.smartcampus.backend.config;

import com.smartcampus.backend.model.Utilisateur;
import com.smartcampus.backend.repository.UtilisateurRepository;
import org.springframework.boot.CommandLineRunner;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;


@Component
public class DataInitializer implements CommandLineRunner {

    private static final String EMAIL_DEFAUT = "siwarmrayhi0@gmail.com";
    private static final String MOT_DE_PASSE_DEFAUT = "ChangeMoi123!";

    private final UtilisateurRepository utilisateurRepository;
    private final PasswordEncoder passwordEncoder;

    public DataInitializer(UtilisateurRepository utilisateurRepository, PasswordEncoder passwordEncoder) {
        this.utilisateurRepository = utilisateurRepository;
        this.passwordEncoder = passwordEncoder;
    }

    @Override
    public void run(String... args) {
        if (utilisateurRepository.count() == 0) {
            Utilisateur admin = new Utilisateur();
            admin.setNom("Administrateur");
            admin.setPrenom("Principal");
            admin.setEmail(EMAIL_DEFAUT);
            admin.setMotDePasseHache(passwordEncoder.encode(MOT_DE_PASSE_DEFAUT));
            admin.setRole("ADMIN");
            utilisateurRepository.save(admin);

            System.out.println("=========================================================");
            System.out.println("Compte admin par défaut créé :");
            System.out.println("  Email      : " + EMAIL_DEFAUT);
            System.out.println("  Mot de passe : " + MOT_DE_PASSE_DEFAUT);
            System.out.println("  A CHANGER dès la première connexion !");
            System.out.println("=========================================================");
        }
    }
}